"""Tests for the classifier — Anthropic client mocked."""
from __future__ import annotations

import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from sable_kol import classify as classify_mod
from sable_kol.db import (
    get_candidate_by_handle,
    list_unclassified,
    upsert_candidate,
)


def _mock_client(payload_by_batch):
    """Return an object with a .messages.create() that yields the next payload.

    payload_by_batch: list of dicts shaped {handle: {archetype_tags, sector_tags, status}}.
    """
    iterator = iter(payload_by_batch)

    def _create(**_kwargs):
        payload = next(iterator)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=json.dumps(payload))],
            usage=SimpleNamespace(input_tokens=100, output_tokens=50),
        )

    return SimpleNamespace(messages=SimpleNamespace(create=_create))


def _patch_open_db(monkeypatch, conn):
    @contextmanager
    def _fake():
        yield conn

    monkeypatch.setattr(classify_mod, "open_db", _fake)


def test_classify_writes_tags_and_status(db_conn, monkeypatch):
    upsert_candidate(db_conn, handle="alice", bio_snapshot="DeFi yield researcher", discovery_source="cahit")
    upsert_candidate(db_conn, handle="bob", bio_snapshot="solana dev", discovery_source="cahit")

    _patch_open_db(monkeypatch, db_conn)

    payload = {
        "alice": {"archetype_tags": ["researcher"], "sector_tags": ["defi"], "status": "active"},
        "bob":   {"archetype_tags": ["dev"],         "sector_tags": ["sol"],  "status": "active"},
    }
    summary = classify_mod.run_classify(client=_mock_client([payload]))
    assert summary.classified == 2
    assert summary.dropped == 0
    assert summary.errors == 0
    assert summary.cost_usd > 0  # token-based estimate fired

    alice = get_candidate_by_handle(db_conn, "alice")
    assert alice.archetype_tags == ["researcher"]
    assert alice.sector_tags == ["defi"]
    assert alice.status == "active"


def test_classify_drops_invalid_tags(db_conn, monkeypatch):
    upsert_candidate(db_conn, handle="weird", discovery_source="cahit")
    _patch_open_db(monkeypatch, db_conn)

    payload = {
        "weird": {
            "archetype_tags": ["thought_leader", "not_a_real_tag"],
            "sector_tags": ["defi", "fake_sector"],
            "status": "active",
        },
    }
    classify_mod.run_classify(client=_mock_client([payload]))
    weird = get_candidate_by_handle(db_conn, "weird")
    assert weird.archetype_tags == ["thought_leader"]
    assert weird.sector_tags == ["defi"]


def test_classify_coerces_invalid_status_to_active(db_conn, monkeypatch):
    upsert_candidate(db_conn, handle="status_oops", discovery_source="cahit")
    _patch_open_db(monkeypatch, db_conn)
    payload = {"status_oops": {"archetype_tags": [], "sector_tags": [], "status": "vibes"}}
    classify_mod.run_classify(client=_mock_client([payload]))
    row = get_candidate_by_handle(db_conn, "status_oops")
    assert row.status == "active"


def test_classify_marks_dropped(db_conn, monkeypatch):
    upsert_candidate(db_conn, handle="botspam", bio_snapshot="follow for free crypto", discovery_source="cahit")
    _patch_open_db(monkeypatch, db_conn)
    payload = {"botspam": {"archetype_tags": [], "sector_tags": [], "status": "drop"}}
    summary = classify_mod.run_classify(client=_mock_client([payload]))
    assert summary.dropped == 1
    row = get_candidate_by_handle(db_conn, "botspam")
    assert row.status == "drop"


def test_classify_skips_already_classified_unless_force(db_conn, monkeypatch):
    upsert_candidate(db_conn, handle="done", discovery_source="cahit")
    _patch_open_db(monkeypatch, db_conn)
    classify_mod.run_classify(
        client=_mock_client([{"done": {"archetype_tags": ["dev"], "sector_tags": ["sol"], "status": "active"}}])
    )
    # Second run finds nothing to do.
    summary = classify_mod.run_classify(client=_mock_client([{}]))
    assert summary.classified == 0
    assert summary.batches == 0


def test_classify_logs_cost_event(db_conn, monkeypatch):
    upsert_candidate(db_conn, handle="alice", discovery_source="cahit")
    _patch_open_db(monkeypatch, db_conn)
    payload = {"alice": {"archetype_tags": ["researcher"], "sector_tags": ["defi"], "status": "active"}}
    classify_mod.run_classify(client=_mock_client([payload]))

    ev = db_conn.execute(
        "SELECT * FROM cost_events WHERE call_type = 'sablekol.anthropic_haiku_classify'"
    ).fetchone()
    assert ev is not None
    assert ev["org_id"] == "_external"  # uses sentinel since classify is org-agnostic
    assert ev["input_tokens"] == 100
    assert ev["output_tokens"] == 50


def test_classify_handles_fenced_json(db_conn, monkeypatch):
    upsert_candidate(db_conn, handle="alice", discovery_source="cahit")
    _patch_open_db(monkeypatch, db_conn)

    # Mock client returns JSON wrapped in markdown fences.
    fenced = "```json\n" + json.dumps({"alice": {"archetype_tags": ["dev"], "sector_tags": ["sol"], "status": "active"}}) + "\n```"

    def _create(**_kwargs):
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=fenced)],
            usage=SimpleNamespace(input_tokens=10, output_tokens=10),
        )

    client = SimpleNamespace(messages=SimpleNamespace(create=_create))
    summary = classify_mod.run_classify(client=client)
    assert summary.classified == 1


def test_classify_no_unclassified_returns_empty_summary(db_conn, monkeypatch):
    _patch_open_db(monkeypatch, db_conn)
    summary = classify_mod.run_classify(client=_mock_client([]))
    assert summary.classified == 0
    assert summary.batches == 0
