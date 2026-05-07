"""Tests for diagnostics — bank stats + conflict resolution."""
from __future__ import annotations

from contextlib import contextmanager

from sable_kol import diagnostics as diag_mod
from sable_kol.db import (
    get_candidate_by_handle,
    list_open_conflicts,
    update_classification,
    upsert_candidate,
)


def _patch_open(monkeypatch, conn):
    @contextmanager
    def _fake():
        yield conn

    monkeypatch.setattr(diag_mod, "open_db", _fake)


# ---------------------------------------------------------------------------
# bank stats
# ---------------------------------------------------------------------------

def test_print_bank_stats_runs(db_conn, monkeypatch, capsys):
    upsert_candidate(db_conn, handle="alice", discovery_source="cahit_list")
    upsert_candidate(db_conn, handle="bob", discovery_source="manual")
    _patch_open(monkeypatch, db_conn)
    diag_mod.print_bank_stats()
    out = capsys.readouterr().out
    assert "Total live candidates : 2" in out
    assert "cahit_list" in out
    assert "manual" in out


def test_source_distribution_counts_multisource_rows(db_conn):
    upsert_candidate(db_conn, handle="alice", discovery_source="cahit_list")
    upsert_candidate(db_conn, handle="alice", discovery_source="org:tig")
    upsert_candidate(db_conn, handle="bob", discovery_source="cahit_list")
    counts = diag_mod._source_distribution(db_conn)
    assert counts["cahit_list"] == 2
    assert counts["org:tig"] == 1


# ---------------------------------------------------------------------------
# bank dump
# ---------------------------------------------------------------------------

def test_bank_dump_prints_row(db_conn, monkeypatch, capsys):
    upsert_candidate(db_conn, handle="zara", display_name="Zara", discovery_source="cahit")
    _patch_open(monkeypatch, db_conn)
    diag_mod.print_bank_row("zara")
    out = capsys.readouterr().out
    assert '"handle_normalized": "zara"' in out
    assert '"display_name": "Zara"' in out


def test_bank_dump_missing_handle_exits(db_conn, monkeypatch):
    _patch_open(monkeypatch, db_conn)
    import pytest
    with pytest.raises(SystemExit):
        diag_mod.print_bank_row("nosuch")


# ---------------------------------------------------------------------------
# conflict resolution
# ---------------------------------------------------------------------------

def _seed_conflict(db_conn) -> int:
    upsert_candidate(db_conn, handle="dup", twitter_id="111", discovery_source="a")
    res = upsert_candidate(db_conn, handle="dup", twitter_id="222", discovery_source="b")
    assert res.conflicted is True
    return res.conflict_id


def test_resolve_discard_drops_incoming(db_conn, monkeypatch):
    cid = _seed_conflict(db_conn)
    _patch_open(monkeypatch, db_conn)
    diag_mod.resolve_conflict(cid, "discard")
    # Live row still has twitter_id=111
    live = get_candidate_by_handle(db_conn, "dup")
    assert live.twitter_id == "111"
    # Conflict is resolved
    assert list_open_conflicts(db_conn) == []
    # Incoming row soft-dropped (not deleted, FK from conflicts still resolves).
    dropped = db_conn.execute(
        "SELECT status, is_unresolved FROM kol_candidates "
        "WHERE handle_normalized='dup' AND twitter_id='222'"
    ).fetchone()
    assert dropped["status"] == "dropped"
    assert dropped["is_unresolved"] == 1


def test_resolve_supersede_swaps_live_status(db_conn, monkeypatch):
    cid = _seed_conflict(db_conn)
    _patch_open(monkeypatch, db_conn)
    diag_mod.resolve_conflict(cid, "supersede")
    live = get_candidate_by_handle(db_conn, "dup")
    assert live.twitter_id == "222"  # the incoming is now live
    # Old live row is now unresolved
    unresolved = db_conn.execute(
        "SELECT twitter_id FROM kol_candidates "
        "WHERE handle_normalized='dup' AND is_unresolved=1"
    ).fetchone()
    assert unresolved["twitter_id"] == "111"


def test_resolve_merge_folds_into_existing(db_conn, monkeypatch):
    cid = _seed_conflict(db_conn)
    _patch_open(monkeypatch, db_conn)
    diag_mod.resolve_conflict(cid, "merge")
    live = get_candidate_by_handle(db_conn, "dup")
    assert live.twitter_id == "111"  # original live row preserved
    # handle_history captures the merged-in row's identity
    assert any(h.get("twitter_id") == "222" for h in live.handle_history)
    # The merged-in row is soft-dropped (not deleted).
    dropped = db_conn.execute(
        "SELECT status, is_unresolved FROM kol_candidates "
        "WHERE handle_normalized='dup' AND twitter_id='222'"
    ).fetchone()
    assert dropped["status"] == "dropped"
    assert dropped["is_unresolved"] == 1


def test_resolve_already_resolved_errors(db_conn, monkeypatch):
    cid = _seed_conflict(db_conn)
    _patch_open(monkeypatch, db_conn)
    diag_mod.resolve_conflict(cid, "discard")
    import pytest
    with pytest.raises(SystemExit):
        diag_mod.resolve_conflict(cid, "discard")  # second time
