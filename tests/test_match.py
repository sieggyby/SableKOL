"""Tests for the matcher — scoring, evidence contract, end-to-end."""
from __future__ import annotations

import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from sable_kol import match as match_mod
from sable_kol.db import (
    Candidate,
    update_classification,
    update_relationship,
    upsert_candidate,
)
from sable_kol.profile import Profile


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _profile(**kw) -> Profile:
    base = dict(source="org", org_id="tig", sector="DeFi", sectors=["DeFi"], themes=[], top_tags=[])
    base.update(kw)
    return Profile(**base)


def _candidate(**kw) -> Candidate:
    base = dict(
        candidate_id=1,
        twitter_id=None,
        handle_normalized="x",
        is_unresolved=0,
        handle_history=[],
        display_name="X",
        bio_snapshot="DeFi yield researcher",
        followers_snapshot=10000,
        discovery_sources=["cahit_list"],
        first_seen_at=None,
        last_seen_at=None,
        archetype_tags=["thought_leader"],
        sector_tags=["defi"],
        sable_relationship={"communities": [], "operators": []},
        enrichment_tier="none",
        last_enriched_at=None,
        status="active",
        manual_notes=None,
        kol_strength_score=None,
        verified=0,
        account_created_at=None,
    )
    base.update(kw)
    return Candidate(**base)


def test_sector_overlap_full_match():
    p = _profile(sectors=["DeFi"])
    c = _candidate(sector_tags=["defi"])
    bd = match_mod.score_candidate(c, p)
    assert bd.sector_overlap == 1.0


def test_sector_overlap_partial_match():
    p = _profile(sectors=["DeFi", "Gaming"])
    c = _candidate(sector_tags=["defi"])
    bd = match_mod.score_candidate(c, p)
    assert bd.sector_overlap == 0.5


def test_sector_overlap_no_match():
    p = _profile(sectors=["Gaming"])
    c = _candidate(sector_tags=["defi"])
    bd = match_mod.score_candidate(c, p)
    assert bd.sector_overlap == 0.0


def test_archetype_match_preferred_full():
    bd = match_mod.score_candidate(
        _candidate(archetype_tags=["connector"]),
        _profile(),
    )
    assert bd.archetype_match == 1.0


def test_archetype_match_non_preferred_half():
    bd = match_mod.score_candidate(
        _candidate(archetype_tags=["trader"]),
        _profile(),
    )
    assert bd.archetype_match == 0.5


def test_archetype_match_unclassified_zero():
    bd = match_mod.score_candidate(
        _candidate(archetype_tags=[]),
        _profile(),
    )
    assert bd.archetype_match == 0.0


def test_sable_relationship_full_when_both():
    bd = match_mod.score_candidate(
        _candidate(sable_relationship={
            "communities": [{"org_id": "tig"}],
            "operators": [{"name": "alice", "relation": "follows"}],
        }),
        _profile(),
    )
    assert bd.sable_relationship == 1.0


def test_sable_relationship_partial_when_one():
    bd = match_mod.score_candidate(
        _candidate(sable_relationship={
            "communities": [{"org_id": "tig"}],
            "operators": [],
        }),
        _profile(),
    )
    assert bd.sable_relationship == 0.7


def test_centrality_proxy_counts_org_sources():
    bd = match_mod.score_candidate(
        _candidate(discovery_sources=["org:tig", "org:psy", "org:multisynq", "cahit_list"]),
        _profile(),
    )
    # 3 org sources / 3.0 = 1.0
    assert bd.centrality_proxy == 1.0


def test_bio_keyword_sim_overlap():
    p = _profile(themes=["yield", "rwa", "treasury"])
    c = _candidate(bio_snapshot="rwa yield aggregator on Solana, treasury management")
    bd = match_mod.score_candidate(c, p)
    assert bd.bio_keyword_sim > 0


def test_weighted_total_in_zero_one_range():
    bd = match_mod.SignalBreakdown(
        sector_overlap=1.0, archetype_match=1.0, bio_keyword_sim=1.0,
        sable_relationship=1.0, centrality_proxy=1.0, kol_strength=1.0,
    )
    assert abs(bd.weighted_total() - 1.0) < 1e-9


def test_kol_strength_uses_stored_value_when_present():
    """If kol_strength_score is set on the row, use it directly."""
    c = _candidate(kol_strength_score=0.77)
    bd = match_mod.score_candidate(c, _profile())
    assert bd.kol_strength == 0.77


def test_kol_strength_falls_back_to_compute_when_null():
    """Pre-enrichment rows fall back to on-the-fly compute_kol_strength."""
    c = _candidate(
        kol_strength_score=None,
        followers_snapshot=1_000_000,
        discovery_sources=["list:cobie:1", "list:rookie:2"],
    )
    bd = match_mod.score_candidate(c, _profile())
    assert bd.kol_strength > 0  # nontrivial because followers + 2 list votes


def test_kol_strength_zero_when_no_signals_and_no_stored():
    c = _candidate(
        kol_strength_score=None,
        followers_snapshot=None,
        discovery_sources=["cahit_list"],   # not list:* prefix
    )
    bd = match_mod.score_candidate(c, _profile())
    assert bd.kol_strength == 0.0


# ---------------------------------------------------------------------------
# Evidence contract validation
# ---------------------------------------------------------------------------

def test_validate_used_keys_passes_known():
    evidence = {"sector_tags": ["defi"], "sable_relationship": {"communities": []}}
    bad = match_mod._validate_used_keys(["sector_tags", "sable_relationship.communities"], evidence)
    assert bad == []


def test_validate_used_keys_rejects_unknown():
    evidence = {"sector_tags": ["defi"]}
    bad = match_mod._validate_used_keys(["sector_tags", "engagement_rate"], evidence)
    assert bad == ["engagement_rate"]


def test_fabrication_denylist_catches_phrases():
    assert match_mod._has_fabrication_phrase("High reply rate and active poster") is not None
    assert match_mod._has_fabrication_phrase("Posts frequently in DeFi spaces") is not None
    assert match_mod._has_fabrication_phrase("Strong sector match") is None


# ---------------------------------------------------------------------------
# Rationale-with-retry
# ---------------------------------------------------------------------------

def _haiku_responder(payloads: list[dict]):
    """Return a fake Anthropic client that yields each payload in turn."""
    iterator = iter(payloads)

    def _create(**_kwargs):
        payload = next(iterator)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=json.dumps(payload))],
            usage=SimpleNamespace(input_tokens=200, output_tokens=80),
        )

    return SimpleNamespace(messages=SimpleNamespace(create=_create))


def test_rationale_passes_first_try_when_clean():
    client = _haiku_responder([
        {"score": 80, "rationale": "Strong DeFi sector alignment", "used_evidence_keys": ["sector_tags"]},
    ])
    evidence = {"sector_tags": ["defi"]}
    parsed, used, _, _, violations = match_mod._rationale_with_retry(
        client,
        project_profile={"sector": "DeFi"},
        candidate_evidence=evidence,
        model="haiku",
    )
    assert violations == 0
    assert parsed["score"] == 80
    assert used == ["sector_tags"]


def test_rationale_retries_on_unknown_key_then_passes():
    client = _haiku_responder([
        {"score": 70, "rationale": "Active in DeFi", "used_evidence_keys": ["engagement_rate"]},
        {"score": 70, "rationale": "DeFi-tagged", "used_evidence_keys": ["sector_tags"]},
    ])
    evidence = {"sector_tags": ["defi"]}
    parsed, used, _, _, violations = match_mod._rationale_with_retry(
        client,
        project_profile={},
        candidate_evidence=evidence,
        model="haiku",
    )
    assert violations == 1
    assert used == ["sector_tags"]
    assert parsed["score"] == 70


def test_rationale_retries_on_denylist_phrase_then_passes():
    client = _haiku_responder([
        {"score": 80, "rationale": "High reply rate in DeFi spaces", "used_evidence_keys": ["sector_tags"]},
        {"score": 80, "rationale": "DeFi sector tagged", "used_evidence_keys": ["sector_tags"]},
    ])
    evidence = {"sector_tags": ["defi"]}
    _, _, _, _, violations = match_mod._rationale_with_retry(
        client,
        project_profile={},
        candidate_evidence=evidence,
        model="haiku",
    )
    assert violations == 1


def test_rationale_two_failures_returns_violation_count_two():
    client = _haiku_responder([
        {"score": 80, "rationale": "High engagement rate", "used_evidence_keys": ["engagement_rate"]},
        {"score": 80, "rationale": "Active poster, posts frequently", "used_evidence_keys": ["activity_rate"]},
    ])
    evidence = {"sector_tags": ["defi"]}
    _, _, _, _, violations = match_mod._rationale_with_retry(
        client,
        project_profile={},
        candidate_evidence=evidence,
        model="haiku",
    )
    assert violations == 2


# ---------------------------------------------------------------------------
# run_find — end to end on path (i)
# ---------------------------------------------------------------------------

def _patch_open(monkeypatch, conn):
    @contextmanager
    def _fake():
        yield conn

    monkeypatch.setattr(match_mod, "open_db", _fake)


def _seed_full_org(conn, org_id="tig"):
    conn.execute(
        "INSERT INTO orgs (org_id, display_name, twitter_handle, config_json) "
        "VALUES (:id, :name, :h, :cfg)",
        {
            "id": org_id, "name": org_id.upper(), "h": "tigfoundation",
            "cfg": json.dumps({"sector": "DeFi", "stage": "growth", "themes": ["yield"]}),
        },
    )
    conn.commit()


def test_run_find_path_i_orders_by_score(db_conn, monkeypatch):
    _seed_full_org(db_conn)
    # Three candidates with varying alignment.
    upsert_candidate(db_conn, handle="alice", bio_snapshot="DeFi yield researcher", discovery_source="cahit")
    update_classification(
        db_conn,
        candidate_id=1,
        archetype_tags=["thought_leader", "researcher"],
        sector_tags=["defi"],
        status="active",
    )
    upsert_candidate(db_conn, handle="bob", bio_snapshot="Memes only", discovery_source="cahit")
    update_classification(
        db_conn,
        candidate_id=2,
        archetype_tags=["trader"],
        sector_tags=["memes"],
        status="active",
    )
    upsert_candidate(db_conn, handle="carol", bio_snapshot="Solana DeFi connector", discovery_source="cahit")
    update_classification(
        db_conn,
        candidate_id=3,
        archetype_tags=["connector"],
        sector_tags=["defi", "sol"],
        status="active",
    )

    _patch_open(monkeypatch, db_conn)
    # Per-handle Haiku response — bob (memes, off-sector) gets a low score even
    # if Haiku is invoked on him; alice and carol get high scores.
    def _by_handle_client(score_by_handle: dict[int, int]):
        # The matcher feeds top-K by rule-prerank order. We can't know that
        # order in advance, so respond based on the request body's handle.
        def _create(**kw):
            user_msg = kw["messages"][0]["content"]
            for handle, score in score_by_handle.items():
                if f'"handle": "{handle}"' in user_msg:
                    payload = {
                        "score": score,
                        "rationale": "Sector tag analysis",
                        "used_evidence_keys": ["sector_tags"],
                    }
                    return SimpleNamespace(
                        content=[SimpleNamespace(type="text", text=json.dumps(payload))],
                        usage=SimpleNamespace(input_tokens=200, output_tokens=80),
                    )
            raise AssertionError(f"unexpected handle in: {user_msg[:200]}")

        return SimpleNamespace(messages=SimpleNamespace(create=_create))

    out = match_mod.run_find(
        org_id="tig",
        haiku_client=_by_handle_client({"alice": 90, "carol": 85, "bob": 30}),
        write_output=False,
    )
    assert out.candidates_considered == 3
    assert out.k_evaluated == 3
    handles = [r.handle for r in out.results]
    # bob is last; alice + carol take the top two slots in some order.
    assert handles[-1] == "bob"
    assert set(handles[:2]) == {"alice", "carol"}


def test_run_find_logs_cost_events(db_conn, monkeypatch):
    _seed_full_org(db_conn)
    upsert_candidate(db_conn, handle="alice", bio_snapshot="DeFi", discovery_source="cahit")
    update_classification(
        db_conn, candidate_id=1, archetype_tags=["thought_leader"],
        sector_tags=["defi"], status="active",
    )
    _patch_open(monkeypatch, db_conn)
    out = match_mod.run_find(
        org_id="tig",
        haiku_client=_haiku_responder([
            {"score": 90, "rationale": "DeFi match", "used_evidence_keys": ["sector_tags"]},
        ]),
        write_output=False,
    )
    assert out.cost_usd > 0
    rows = db_conn.execute(
        "SELECT call_type, org_id FROM cost_events WHERE call_type LIKE 'sablekol.%'"
    ).fetchall()
    types = {r["call_type"] for r in rows}
    assert "sablekol.anthropic_haiku_rationale" in types
    # Logged against the org_id since we used path (i).
    assert any(r["org_id"] == "tig" for r in rows)


def test_run_find_degrades_on_persistent_evidence_violation(db_conn, monkeypatch):
    _seed_full_org(db_conn)
    upsert_candidate(db_conn, handle="alice", bio_snapshot="DeFi", discovery_source="cahit")
    update_classification(
        db_conn, candidate_id=1, archetype_tags=["thought_leader"],
        sector_tags=["defi"], status="active",
    )
    _patch_open(monkeypatch, db_conn)
    out = match_mod.run_find(
        org_id="tig",
        haiku_client=_haiku_responder([
            {"score": 99, "rationale": "High engagement rate", "used_evidence_keys": ["engagement_rate"]},
            {"score": 99, "rationale": "Active poster with reply rate signal", "used_evidence_keys": ["activity_rate"]},
        ]),
        write_output=False,
    )
    assert out.evidence_violations == 2
    r = out.results[0]
    assert r.rationale == "<excluded due to evidence violation>"
    # Score falls back to rule prerank (NOT the bogus Haiku 99).
    assert r.score == r.rule_prerank_score


def test_run_find_excludes_unclassified(db_conn, monkeypatch):
    _seed_full_org(db_conn)
    # alice classified, bob NOT.
    upsert_candidate(db_conn, handle="alice", discovery_source="cahit")
    update_classification(
        db_conn, candidate_id=1, archetype_tags=["dev"], sector_tags=["defi"], status="active",
    )
    upsert_candidate(db_conn, handle="bob", discovery_source="cahit")
    _patch_open(monkeypatch, db_conn)
    out = match_mod.run_find(
        org_id="tig",
        haiku_client=_haiku_responder([
            {"score": 70, "rationale": "DeFi", "used_evidence_keys": ["sector_tags"]},
        ]),
        write_output=False,
    )
    assert out.candidates_considered == 1
    assert {r.handle for r in out.results} == {"alice"}


def test_run_find_external_handle_path(db_conn, monkeypatch):
    upsert_candidate(db_conn, handle="alice", bio_snapshot="DeFi", discovery_source="cahit")
    update_classification(
        db_conn, candidate_id=1, archetype_tags=["thought_leader"],
        sector_tags=["defi"], status="active",
    )
    _patch_open(monkeypatch, db_conn)
    out = match_mod.run_find(
        external_handle="newproject",
        sector="DeFi",
        themes=["yield"],
        paid_enrich=False,
        haiku_client=_haiku_responder([
            {"score": 88, "rationale": "DeFi tag match", "used_evidence_keys": ["sector_tags"]},
        ]),
        write_output=False,
    )
    assert out.project["source"] == "external_manual"
    assert out.project["sector"] == "DeFi"


def test_run_find_with_no_candidates_returns_empty(db_conn, monkeypatch):
    _seed_full_org(db_conn)
    _patch_open(monkeypatch, db_conn)
    out = match_mod.run_find(
        org_id="tig",
        haiku_client=_haiku_responder([]),
        write_output=False,
    )
    assert out.candidates_considered == 0
    assert out.k_evaluated == 0
    assert out.results == []


def test_run_find_respects_limit(db_conn, monkeypatch):
    _seed_full_org(db_conn)
    for i in range(5):
        h = f"u{i}"
        upsert_candidate(db_conn, handle=h, bio_snapshot="DeFi", discovery_source="cahit")
        cand_id = db_conn.execute(
            "SELECT candidate_id FROM kol_candidates WHERE handle_normalized = :h",
            {"h": h},
        ).fetchone()["candidate_id"]
        update_classification(
            db_conn, candidate_id=cand_id, archetype_tags=["dev"],
            sector_tags=["defi"], status="active",
        )
    _patch_open(monkeypatch, db_conn)
    payloads = [
        {"score": 50 + i, "rationale": "DeFi", "used_evidence_keys": ["sector_tags"]}
        for i in range(5)
    ]
    out = match_mod.run_find(
        org_id="tig",
        limit=2,
        haiku_client=_haiku_responder(payloads),
        write_output=False,
    )
    assert len(out.results) == 2
