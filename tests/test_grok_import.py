"""Tests for the Grok-import path."""
from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

import pytest

from sable_kol import grok_import as gi_mod
from sable_kol.db import (
    get_candidate_by_handle,
    update_classification,
    upsert_candidate,
)


def _patch_open(monkeypatch, conn):
    @contextmanager
    def _fake():
        yield conn
    monkeypatch.setattr(gi_mod, "open_db", _fake)


# ---------------------------------------------------------------------------
# parse_grok_json
# ---------------------------------------------------------------------------

def test_parse_clean_json():
    text = json.dumps([{"handle": "alice"}, {"handle": "bob"}])
    out = gi_mod.parse_grok_json(text)
    assert len(out) == 2


def test_parse_strips_markdown_fences():
    text = "```json\n" + json.dumps([{"handle": "alice"}]) + "\n```"
    out = gi_mod.parse_grok_json(text)
    assert out == [{"handle": "alice"}]


def test_parse_recovers_from_truncated_array():
    """Truncation mid-object — recover the complete objects before the break."""
    truncated = (
        '[{"handle": "alice", "bio": "x"}, '
        '{"handle": "bob", "bio": "y"}, '
        '{"handle": "carol", "bi'  # cut off mid-key
    )
    out = gi_mod.parse_grok_json(truncated)
    assert len(out) == 2
    assert out[0]["handle"] == "alice"
    assert out[1]["handle"] == "bob"


def test_parse_handles_empty_array():
    assert gi_mod.parse_grok_json("[]") == []


def test_parse_handles_single_object():
    out = gi_mod.parse_grok_json(json.dumps({"handle": "alice"}))
    assert out == [{"handle": "alice"}]


# ---------------------------------------------------------------------------
# run_grok_import — end-to-end
# ---------------------------------------------------------------------------

def _make_grok_entry(**kw):
    base = {
        "handle": "alice", "twitter_id": None, "followers": None, "following": None,
        "tweets_count": None, "listed_count": None, "verified": None,
        "account_created": None, "bio": None, "is_active": True,
        "primary_archetype": None, "primary_sectors": None,
        "credibility_signal": None, "real_name_known": None, "notes": None,
    }
    base.update(kw)
    return base


def test_import_updates_bio_when_grok_has_better(tmp_path, db_conn, monkeypatch):
    upsert_candidate(db_conn, handle="alice", bio_snapshot="short", discovery_source="x")
    update_classification(
        db_conn, candidate_id=1, archetype_tags=["dev"],
        sector_tags=["defi"], status="active",
    )
    _patch_open(monkeypatch, db_conn)

    f = tmp_path / "g.json"
    f.write_text(json.dumps([_make_grok_entry(handle="alice", bio="A much longer canonical bio from Grok")]))
    s = gi_mod.run_grok_import(f)
    assert s.parsed == 1
    assert s.updated == 1
    assert s.rescored == 1
    cand = get_candidate_by_handle(db_conn, "alice")
    assert "longer canonical bio" in cand.bio_snapshot


def test_import_keeps_existing_bio_when_grok_is_shorter(tmp_path, db_conn, monkeypatch):
    upsert_candidate(db_conn, handle="alice", bio_snapshot="A nice long existing bio that's already informative", discovery_source="x")
    update_classification(db_conn, candidate_id=1, archetype_tags=["dev"], sector_tags=["defi"], status="active")
    _patch_open(monkeypatch, db_conn)

    f = tmp_path / "g.json"
    f.write_text(json.dumps([_make_grok_entry(handle="alice", bio="short")]))
    gi_mod.run_grok_import(f)
    cand = get_candidate_by_handle(db_conn, "alice")
    assert "nice long existing bio" in cand.bio_snapshot  # unchanged


def test_import_writes_followers_and_verified(tmp_path, db_conn, monkeypatch):
    upsert_candidate(db_conn, handle="alice", discovery_source="x")
    update_classification(db_conn, candidate_id=1, archetype_tags=["dev"], sector_tags=["defi"], status="active")
    _patch_open(monkeypatch, db_conn)

    f = tmp_path / "g.json"
    f.write_text(json.dumps([
        _make_grok_entry(handle="alice", followers=250000, verified=True, twitter_id="999"),
    ]))
    gi_mod.run_grok_import(f)
    row = db_conn.execute(
        "SELECT followers_snapshot, verified, twitter_id, enrichment_tier FROM kol_candidates "
        "WHERE handle_normalized='alice'"
    ).fetchone()
    assert row["followers_snapshot"] == 250000
    assert row["verified"] == 1
    assert row["twitter_id"] == "999"
    assert row["enrichment_tier"] == "grok_basic"


def test_import_writes_grok_specific_fields(tmp_path, db_conn, monkeypatch):
    upsert_candidate(db_conn, handle="alice", discovery_source="x")
    update_classification(db_conn, candidate_id=1, archetype_tags=["dev"], sector_tags=["defi"], status="active")
    _patch_open(monkeypatch, db_conn)

    f = tmp_path / "g.json"
    f.write_text(json.dumps([
        _make_grok_entry(
            handle="alice",
            listed_count=12345, tweets_count=8000, following=420,
            credibility_signal="high", real_name_known=True,
            notes="DeFi researcher at Paradigm",
        ),
    ]))
    gi_mod.run_grok_import(f)
    row = db_conn.execute(
        "SELECT listed_count, tweets_count, following_count, credibility_signal, "
        "       real_name_known, notes FROM kol_candidates WHERE handle_normalized='alice'"
    ).fetchone()
    assert row["listed_count"] == 12345
    assert row["tweets_count"] == 8000
    assert row["following_count"] == 420
    assert row["credibility_signal"] == "high"
    assert row["real_name_known"] == 1
    assert row["notes"] == "DeFi researcher at Paradigm"


def test_import_marks_dormant_when_is_active_false(tmp_path, db_conn, monkeypatch):
    upsert_candidate(db_conn, handle="dead", discovery_source="x")
    update_classification(db_conn, candidate_id=1, archetype_tags=["dev"], sector_tags=["defi"], status="active")
    _patch_open(monkeypatch, db_conn)

    f = tmp_path / "g.json"
    f.write_text(json.dumps([_make_grok_entry(handle="dead", is_active=False)]))
    gi_mod.run_grok_import(f)
    row = db_conn.execute(
        "SELECT status FROM kol_candidates WHERE handle_normalized='dead'"
    ).fetchone()
    assert row["status"] == "dormant"


def test_import_merges_archetype_and_sectors_into_existing(tmp_path, db_conn, monkeypatch):
    upsert_candidate(db_conn, handle="alice", discovery_source="x")
    update_classification(
        db_conn, candidate_id=1, archetype_tags=["dev"],
        sector_tags=["defi"], status="active",
    )
    _patch_open(monkeypatch, db_conn)

    f = tmp_path / "g.json"
    f.write_text(json.dumps([_make_grok_entry(
        handle="alice",
        primary_archetype="researcher",
        primary_sectors=["sol", "ai"],
    )]))
    gi_mod.run_grok_import(f)
    cand = get_candidate_by_handle(db_conn, "alice")
    assert set(cand.archetype_tags) == {"dev", "researcher"}
    assert set(cand.sector_tags) == {"defi", "sol", "ai"}


def test_import_rejects_unknown_archetype(tmp_path, db_conn, monkeypatch):
    upsert_candidate(db_conn, handle="alice", discovery_source="x")
    update_classification(db_conn, candidate_id=1, archetype_tags=["dev"], sector_tags=["defi"], status="active")
    _patch_open(monkeypatch, db_conn)

    f = tmp_path / "g.json"
    f.write_text(json.dumps([_make_grok_entry(handle="alice", primary_archetype="madeup_role")]))
    gi_mod.run_grok_import(f)
    cand = get_candidate_by_handle(db_conn, "alice")
    assert cand.archetype_tags == ["dev"]  # unchanged


def test_import_handles_missing_handle(tmp_path, db_conn, monkeypatch):
    _patch_open(monkeypatch, db_conn)
    f = tmp_path / "g.json"
    f.write_text(json.dumps([_make_grok_entry(handle="ghost_who_isnt_in_bank")]))
    s = gi_mod.run_grok_import(f)
    assert s.not_found == 1
    assert s.updated == 0


def test_import_recomputes_kol_strength(tmp_path, db_conn, monkeypatch):
    upsert_candidate(db_conn, handle="alice", discovery_source="list:c1:1")
    update_classification(db_conn, candidate_id=1, archetype_tags=["dev"], sector_tags=["defi"], status="active")
    _patch_open(monkeypatch, db_conn)

    f = tmp_path / "g.json"
    f.write_text(json.dumps([_make_grok_entry(handle="alice", followers=500_000, verified=True)]))
    gi_mod.run_grok_import(f)
    row = db_conn.execute(
        "SELECT kol_strength_score FROM kol_candidates WHERE handle_normalized='alice'"
    ).fetchone()
    assert row["kol_strength_score"] is not None
    assert row["kol_strength_score"] > 0
