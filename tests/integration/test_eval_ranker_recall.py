"""Ranker-recall regression — top-N over the in-bank subset.

Skipped via ``SABLEKOL_SKIP_EVAL=1``. Threshold is configurable via env var
``SABLEKOL_RECALL50_THRESHOLD`` (default 0.0 — empty gold set passes).
"""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from types import SimpleNamespace

import pytest


pytestmark = pytest.mark.skipif(
    os.environ.get("SABLEKOL_SKIP_EVAL") == "1",
    reason="SABLEKOL_SKIP_EVAL=1",
)


RECALL50_THRESHOLD = float(os.environ.get("SABLEKOL_RECALL50_THRESHOLD", "0.0"))


def _haiku_responder(payloads):
    iterator = iter(payloads)

    def _create(**_kwargs):
        payload = next(iterator)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=json.dumps(payload))],
            usage=SimpleNamespace(input_tokens=200, output_tokens=80),
        )

    return SimpleNamespace(messages=SimpleNamespace(create=_create))


def test_ranker_recall_top50_over_in_bank_subset(db_conn, monkeypatch, tmp_path):
    """Synthetic: 3 in-bank gold KOLs + 1 missing. Matcher returns all 3 in top-N."""
    from sable_kol import eval as eval_mod
    from sable_kol import match as match_mod
    from sable_kol.db import update_classification, upsert_candidate

    gold = tmp_path / "gold.yaml"
    gold.write_text(
        "projects:\n"
        "  - org_id: tig\n"
        "    description: 'TIG'\n"
        "    kols: ['alice', 'bob', 'carol', 'dora_missing']\n"
    )

    db_conn.execute(
        "INSERT INTO orgs (org_id, display_name, twitter_handle, config_json) "
        "VALUES ('tig', 'TIG', 'tigfoundation', :cfg)",
        {"cfg": json.dumps({"sector": "DeFi", "stage": "growth"})},
    )
    db_conn.commit()
    for h in ("alice", "bob", "carol"):
        upsert_candidate(db_conn, handle=h, bio_snapshot="DeFi", discovery_source="cahit")
        cid = db_conn.execute(
            "SELECT candidate_id FROM kol_candidates WHERE handle_normalized = :h",
            {"h": h},
        ).fetchone()["candidate_id"]
        update_classification(
            db_conn,
            candidate_id=cid,
            archetype_tags=["thought_leader"],
            sector_tags=["defi"],
            status="active",
        )

    @contextmanager
    def _fake_open():
        yield db_conn

    monkeypatch.setattr(eval_mod, "open_db", _fake_open)
    monkeypatch.setattr(match_mod, "open_db", _fake_open)

    entries = eval_mod.load_gold_set(gold)
    payloads = [
        {"score": 90 - i, "rationale": "DeFi tag", "used_evidence_keys": ["sector_tags"]}
        for i in range(3)
    ]
    reports = eval_mod.compute_recall(entries, haiku_client=_haiku_responder(payloads))
    assert len(reports) == 1
    r = reports[0]
    assert sorted(r.in_bank_kols) == ["alice", "bob", "carol"]
    assert sorted(r.top50_hits) == ["alice", "bob", "carol"]
    assert r.recall_at_50 == 1.0
    assert r.recall_at_50 >= RECALL50_THRESHOLD


def test_recall_skips_projects_with_no_in_bank_gold(db_conn, monkeypatch, tmp_path):
    """A gold project with no in-bank rows reports vacuously perfect recall."""
    from sable_kol import eval as eval_mod

    gold = tmp_path / "gold.yaml"
    gold.write_text(
        "projects:\n"
        "  - org_id: tig\n"
        "    description: 'TIG'\n"
        "    kols: ['absent_one', 'absent_two']\n"
    )

    @contextmanager
    def _fake_open():
        yield db_conn

    monkeypatch.setattr(eval_mod, "open_db", _fake_open)

    entries = eval_mod.load_gold_set(gold)
    reports = eval_mod.compute_recall(entries)
    assert len(reports) == 1
    assert reports[0].in_bank_kols == []
    assert reports[0].recall_at_50 == 1.0  # vacuous
