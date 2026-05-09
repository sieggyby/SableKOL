"""Tests for the enrich module."""
from __future__ import annotations

from contextlib import contextmanager

import pytest

from sable_kol import enrich as enrich_mod
from sable_kol.db import (
    Candidate,
    update_classification,
    upsert_candidate,
)


def _patch_open_db(monkeypatch, conn):
    @contextmanager
    def _fake():
        yield conn

    monkeypatch.setattr(enrich_mod, "open_db", _fake)


def _activate(db_conn, handle: str) -> None:
    """Make a candidate matchable (active + classified)."""
    cid = db_conn.execute(
        "SELECT candidate_id FROM kol_candidates WHERE handle_normalized = :h",
        {"h": handle},
    ).fetchone()["candidate_id"]
    update_classification(
        db_conn,
        candidate_id=cid,
        archetype_tags=["thought_leader"],
        sector_tags=["defi"],
        status="active",
    )


# ---------------------------------------------------------------------------
# compute_kol_strength
# ---------------------------------------------------------------------------

def _stub_candidate(**kw):
    base = dict(
        candidate_id=1,
        twitter_id=None,
        handle_normalized="alice",
        is_unresolved=0,
        handle_history=[],
        display_name=None,
        bio_snapshot=None,
        followers_snapshot=None,
        discovery_sources=[],
        first_seen_at=None,
        last_seen_at=None,
        archetype_tags=[],
        sector_tags=[],
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


def test_compute_strength_zero_when_no_signals():
    c = _stub_candidate()
    assert enrich_mod.compute_kol_strength(c) == 0.0


def test_compute_strength_followers_only():
    """1M followers ≈ 0.5 of weight (followers_score ~ 0.86 × 0.5 weight)."""
    c = _stub_candidate(followers_snapshot=1_000_000)
    score = enrich_mod.compute_kol_strength(c)
    # log10(1M)=6, mapped via (6-3)/3.5 = 0.857
    # followers_score weight 0.5 → 0.857 * 0.5 ≈ 0.43
    assert 0.40 < score < 0.46


def test_compute_strength_caps_at_one():
    """Maxed inputs produce score ≤ 1.0."""
    c = _stub_candidate(
        followers_snapshot=10_000_000,
        discovery_sources=[f"list:c{i}:{1000+i}" for i in range(10)],
        verified=1,
    )
    score = enrich_mod.compute_kol_strength(c)
    assert 0.95 <= score <= 1.0


def test_compute_strength_list_votes_capped_at_5():
    """6 list votes scores the same as 10 list votes (cap at 5)."""
    c5 = _stub_candidate(discovery_sources=[f"list:c{i}:{1000+i}" for i in range(5)])
    c10 = _stub_candidate(discovery_sources=[f"list:c{i}:{1000+i}" for i in range(10)])
    assert enrich_mod.compute_kol_strength(c5) == enrich_mod.compute_kol_strength(c10)


def test_compute_strength_verified_bonus():
    c_unverified = _stub_candidate(followers_snapshot=10_000)
    c_verified = _stub_candidate(followers_snapshot=10_000, verified=1)
    assert enrich_mod.compute_kol_strength(c_verified) > enrich_mod.compute_kol_strength(c_unverified)


def test_compute_strength_ignores_non_list_sources():
    """org:* and cahit_list (without prefix) don't count toward list_vote_score."""
    c = _stub_candidate(discovery_sources=["org:tig", "cahit_list", "manual"])
    assert enrich_mod.compute_kol_strength(c) == 0.0


# ---------------------------------------------------------------------------
# run_score_only — no paid calls
# ---------------------------------------------------------------------------

def test_score_only_writes_score_for_every_live_active(db_conn, monkeypatch):
    upsert_candidate(db_conn, handle="alice", followers_snapshot=10_000, discovery_source="list:c1:1")
    upsert_candidate(db_conn, handle="bob", followers_snapshot=500_000, discovery_source="list:c1:1")
    _activate(db_conn, "alice")
    _activate(db_conn, "bob")

    _patch_open_db(monkeypatch, db_conn)
    s = enrich_mod.run_score_only()
    assert s.rescored == 2

    rows = db_conn.execute(
        "SELECT handle_normalized, kol_strength_score FROM kol_candidates "
        "WHERE handle_normalized IN ('alice','bob')"
    ).fetchall()
    by_handle = {r["handle_normalized"]: r["kol_strength_score"] for r in rows}
    assert by_handle["alice"] is not None
    assert by_handle["bob"] is not None
    # bob has more followers → higher score
    assert by_handle["bob"] > by_handle["alice"]


def test_score_only_no_paid_calls(db_conn, monkeypatch):
    upsert_candidate(db_conn, handle="alice", discovery_source="list:c1:1")
    _activate(db_conn, "alice")
    _patch_open_db(monkeypatch, db_conn)
    enrich_mod.run_score_only()
    rows = db_conn.execute(
        "SELECT COUNT(*) AS n FROM cost_events WHERE call_type LIKE 'sablekol.%'"
    ).fetchone()
    assert rows["n"] == 0


# ---------------------------------------------------------------------------
# run_enrich — paid path with mock fetcher
# ---------------------------------------------------------------------------

def test_enrich_writes_followers_and_strength(db_conn, monkeypatch):
    upsert_candidate(db_conn, handle="alice", discovery_source="list:c1:1")
    _activate(db_conn, "alice")

    def fetcher(handle):
        return {
            "id_str": "999",
            "name": "Alice",
            "description": "DeFi researcher",
            "followers_count": 250_000,
            "verified": True,
            "created_at": "2018-03-01T00:00:00.000Z",
        }

    _patch_open_db(monkeypatch, db_conn)
    s = enrich_mod.run_enrich(socialdata_fetcher=fetcher)
    assert s.enriched == 1
    assert s.errors == 0
    assert s.cost_usd == pytest.approx(0.0002)
    assert s.rescored == 1

    row = db_conn.execute(
        "SELECT * FROM kol_candidates WHERE handle_normalized='alice'"
    ).fetchone()
    assert row["twitter_id"] == "999"
    assert row["followers_snapshot"] == 250_000
    assert row["verified"] == 1
    assert row["account_created_at"] == "2018-03-01T00:00:00.000Z"
    assert row["enrichment_tier"] == "basic"
    assert row["kol_strength_score"] is not None
    assert row["kol_strength_score"] > 0


def test_enrich_skips_fresh_within_ttl(db_conn, monkeypatch):
    upsert_candidate(db_conn, handle="alice", discovery_source="list:c1:1")
    _activate(db_conn, "alice")
    # Mark the row as recently enriched
    db_conn.execute(
        "UPDATE kol_candidates SET last_enriched_at = datetime('now'), enrichment_tier='basic' "
        "WHERE handle_normalized='alice'"
    )
    db_conn.commit()

    calls = []

    def fetcher(handle):
        calls.append(handle)
        return {"description": "shouldn't fire", "followers_count": 100}

    _patch_open_db(monkeypatch, db_conn)
    s = enrich_mod.run_enrich(socialdata_fetcher=fetcher)
    assert s.enriched == 0
    assert s.skipped_fresh == 1
    assert s.rescored == 1  # score recomputed even when SocialData skipped
    assert calls == []


def test_enrich_refresh_forces_fetch_within_ttl(db_conn, monkeypatch):
    upsert_candidate(db_conn, handle="alice", discovery_source="list:c1:1")
    _activate(db_conn, "alice")
    db_conn.execute(
        "UPDATE kol_candidates SET last_enriched_at = datetime('now'), enrichment_tier='basic' "
        "WHERE handle_normalized='alice'"
    )
    db_conn.commit()

    def fetcher(handle):
        return {"description": "refreshed", "followers_count": 10_000_000}

    _patch_open_db(monkeypatch, db_conn)
    s = enrich_mod.run_enrich(socialdata_fetcher=fetcher, refresh=True)
    assert s.enriched == 1
    assert s.skipped_fresh == 0


def test_enrich_logs_cost_events(db_conn, monkeypatch):
    upsert_candidate(db_conn, handle="alice", discovery_source="list:c1:1")
    _activate(db_conn, "alice")

    def fetcher(_h):
        return {"description": "x", "followers_count": 100}

    _patch_open_db(monkeypatch, db_conn)
    enrich_mod.run_enrich(socialdata_fetcher=fetcher)

    ev = db_conn.execute(
        "SELECT * FROM cost_events WHERE call_type='sablekol.socialdata_user_profile'"
    ).fetchone()
    assert ev is not None
    assert ev["call_status"] == "success"
    assert ev["org_id"] == "_external"


def test_enrich_fetcher_error_logs_failed_cost_event(db_conn, monkeypatch):
    upsert_candidate(db_conn, handle="alice", discovery_source="list:c1:1")
    _activate(db_conn, "alice")

    def boom(_h):
        raise RuntimeError("network down")

    _patch_open_db(monkeypatch, db_conn)
    s = enrich_mod.run_enrich(socialdata_fetcher=boom)
    assert s.enriched == 0
    assert s.errors == 1

    ev = db_conn.execute(
        "SELECT call_status FROM cost_events WHERE call_type='sablekol.socialdata_user_profile'"
    ).fetchone()
    assert ev["call_status"] == "error"


def test_enrich_limit_caps_rows(db_conn, monkeypatch):
    for h in ("a", "b", "c"):
        upsert_candidate(db_conn, handle=h, discovery_source="list:c1:1")
        _activate(db_conn, h)

    calls = []

    def fetcher(h):
        calls.append(h)
        return {"description": "x", "followers_count": 1000}

    _patch_open_db(monkeypatch, db_conn)
    enrich_mod.run_enrich(socialdata_fetcher=fetcher, limit=2)
    assert len(calls) == 2
