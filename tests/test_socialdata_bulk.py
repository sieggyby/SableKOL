"""Tests for sable_kol.socialdata_bulk — cursor pagination, qc, run records."""
from __future__ import annotations

import json
from typing import Any

import pytest

from sable_kol import socialdata_bulk as bulk


# ---------------------------------------------------------------------------
# qc_profile
# ---------------------------------------------------------------------------

def _good_profile(**overrides) -> dict:
    base = {
        "id_str": "12345",
        "screen_name": "alice",
        "followers_count": 5000,
        "friends_count": 200,
        "statuses_count": 1000,
        "description": "A real profile.",
        "protected": False,
    }
    base.update(overrides)
    return base


def test_qc_profile_accepts_complete_profile():
    assert bulk.qc_profile(_good_profile()) is True


def test_qc_profile_rejects_missing_required_field():
    p = _good_profile()
    del p["id_str"]
    assert bulk.qc_profile(p) is False


def test_qc_profile_rejects_protected():
    assert bulk.qc_profile(_good_profile(protected=True)) is False


def test_qc_profile_rejects_suspended_signature():
    """No description + tiny followers + tiny posts = looks suspended/empty."""
    p = _good_profile(description=None, followers_count=10, statuses_count=2)
    assert bulk.qc_profile(p) is False


def test_qc_profile_keeps_low_follower_with_description():
    """Low-follower account is fine if it has a real description."""
    p = _good_profile(followers_count=20, description="real bio")
    assert bulk.qc_profile(p) is True


def test_qc_profile_rejects_non_dict():
    assert bulk.qc_profile(None) is False
    assert bulk.qc_profile("nope") is False


# ---------------------------------------------------------------------------
# Run record CRUD
# ---------------------------------------------------------------------------

def test_create_run_persists_and_get_run_returns(db_conn):
    run = bulk.create_run(
        db_conn,
        target_handle="@DOJI_com",
        target_user_id="12345",
        extract_type="followers",
        expected_count=5691,
    )
    assert run.target_handle_normalized == "doji_com"
    assert run.target_user_id == "12345"
    assert run.cursor_completed == 0
    assert run.expected_count == 5691

    fetched = bulk.get_run(db_conn, run.run_id)
    assert fetched is not None
    assert fetched.target_handle_normalized == "doji_com"
    assert fetched.pages_fetched == 0


def test_create_run_rejects_invalid_extract_type(db_conn):
    with pytest.raises(ValueError):
        bulk.create_run(
            db_conn,
            target_handle="x",
            target_user_id="1",
            extract_type="bogus",
        )


def test_create_run_persists_client_id(db_conn):
    """Migration 039 + Codex round-2 follow-up: create_run must accept and
    write client_id; default '_external' applies when caller omits the arg."""
    run_default = bulk.create_run(
        db_conn,
        target_handle="alice",
        target_user_id="1",
        extract_type="followers",
    )
    run_explicit = bulk.create_run(
        db_conn,
        target_handle="bob",
        target_user_id="2",
        extract_type="followers",
        client_id="solstitch",
    )
    rows = db_conn.execute(
        "SELECT target_handle_normalized, client_id FROM kol_extract_runs ORDER BY started_at"
    ).fetchall()
    by_handle = {r["target_handle_normalized"]: r["client_id"] for r in rows}
    assert by_handle["alice"] == "_external"
    assert by_handle["bob"] == "solstitch"

    # Round-trip via get_run preserves the value.
    fetched_alice = bulk.get_run(db_conn, run_default.run_id)
    fetched_bob = bulk.get_run(db_conn, run_explicit.run_id)
    assert fetched_alice.client_id == "_external"
    assert fetched_bob.client_id == "solstitch"


def test_mark_run_completed_sets_flag(db_conn):
    run = bulk.create_run(
        db_conn, target_handle="x", target_user_id="1", extract_type="followers"
    )
    bulk.mark_run_completed(db_conn, run.run_id)
    fetched = bulk.get_run(db_conn, run.run_id)
    assert fetched.cursor_completed == 1
    assert fetched.partial_failure_reason is None


def test_mark_run_failed_records_reason(db_conn):
    run = bulk.create_run(
        db_conn, target_handle="x", target_user_id="1", extract_type="followers"
    )
    bulk.mark_run_failed(db_conn, run.run_id, "429_rate_limit")
    fetched = bulk.get_run(db_conn, run.run_id)
    assert fetched.partial_failure_reason == "429_rate_limit"
    assert fetched.cursor_completed == 0


# ---------------------------------------------------------------------------
# insert_edges
# ---------------------------------------------------------------------------

def test_insert_edges_dedupes_on_composite_pk(db_conn):
    run = bulk.create_run(
        db_conn, target_handle="t", target_user_id="1", extract_type="following"
    )
    edges = [
        {
            "follower_id": "1",
            "follower_handle": "alice",
            "followed_id": "100",
            "followed_handle": "kingmaker",
        },
        # Same composite PK — should be skipped.
        {
            "follower_id": "1",
            "follower_handle": "alice",
            "followed_id": "100",
            "followed_handle": "kingmaker",
        },
        {
            "follower_id": "2",
            "follower_handle": "bob",
            "followed_id": "100",
            "followed_handle": "kingmaker",
        },
    ]
    inserted = bulk.insert_edges(db_conn, run_id=run.run_id, edges=edges)
    assert inserted == 2
    n = db_conn.execute(
        "SELECT COUNT(*) AS n FROM kol_follow_edges WHERE run_id = :r",
        {"r": run.run_id},
    ).fetchone()["n"]
    assert n == 2


# ---------------------------------------------------------------------------
# resolve_user_id
# ---------------------------------------------------------------------------

def test_resolve_user_id_uses_bank_when_present(db_conn):
    db_conn.execute(
        "INSERT INTO kol_candidates (handle_normalized, twitter_id) VALUES ('alice', '999')"
    )
    db_conn.commit()

    def _fail(_h):
        raise AssertionError("should not call paid endpoint when bank has the id")

    uid = bulk.resolve_user_id(db_conn, "@alice", socialdata_fetcher=_fail)
    assert uid == "999"


def test_resolve_user_id_falls_back_to_paid_call(db_conn):
    calls: list[str] = []

    def _fetcher(handle):
        calls.append(handle)
        return {"id_str": "42", "screen_name": handle}

    uid = bulk.resolve_user_id(db_conn, "newaccount", socialdata_fetcher=_fetcher)
    assert uid == "42"
    assert calls == ["newaccount"]
    # And it logged a cost row.
    rows = db_conn.execute(
        "SELECT call_type FROM cost_events WHERE call_type LIKE '%socialdata%'"
    ).fetchall()
    assert any("profile_resolve" in r[0] for r in rows)


def test_resolve_user_id_logs_cost_on_error(db_conn):
    def _boom(_h):
        raise RuntimeError("network down")

    with pytest.raises(RuntimeError):
        bulk.resolve_user_id(db_conn, "x", socialdata_fetcher=_boom)
    rows = db_conn.execute(
        "SELECT call_type, call_status FROM cost_events"
    ).fetchall()
    assert any(r[1] == "error" for r in rows)


# ---------------------------------------------------------------------------
# pull_followers / pull_following — pagination + checkpoint
# ---------------------------------------------------------------------------

def _page(users: list[dict], cursor: str | None) -> dict:
    return {"users": users, "next_cursor": cursor}


def _make_user(i: int, **overrides) -> dict:
    base = _good_profile(
        id_str=str(1000 + i),
        screen_name=f"user{i}",
        followers_count=10_000,
    )
    base.update(overrides)
    return base


def test_pull_followers_paginates_to_completion(db_conn):
    run = bulk.create_run(
        db_conn,
        target_handle="doji_com",
        target_user_id="555",
        extract_type="followers",
    )
    pages = [
        _page([_make_user(1), _make_user(2)], cursor="cursor_2"),
        _page([_make_user(3)], cursor="cursor_3"),
        _page([_make_user(4)], cursor=None),
    ]
    page_iter = iter(pages)
    seen_params: list[dict] = []

    def fetcher(path, params):
        seen_params.append(dict(params))
        return next(page_iter)

    profiles = list(
        bulk.pull_followers(db_conn, run=run, socialdata_fetcher=fetcher)
    )
    assert len(profiles) == 4
    final = bulk.get_run(db_conn, run.run_id)
    assert final.cursor_completed == 1
    assert final.pages_fetched == 3
    assert final.rows_inserted == 4
    # 4 results total (2 + 1 + 1) × $0.0002 per result.
    assert abs(final.cost_usd_logged - 0.0008) < 1e-9
    # First call had no cursor; subsequent had the prior next_cursor.
    assert "cursor" not in seen_params[0]
    assert seen_params[1]["cursor"] == "cursor_2"
    assert seen_params[2]["cursor"] == "cursor_3"


def test_pull_followers_floor_filter_drops_low_followers(db_conn):
    run = bulk.create_run(
        db_conn,
        target_handle="doji_com",
        target_user_id="555",
        extract_type="followers",
    )
    pages = [_page([
        _make_user(1, followers_count=50),    # below floor
        _make_user(2, followers_count=600),   # above floor (default 500)
    ], cursor=None)]

    def fetcher(path, params):
        return pages.pop(0)

    profiles = list(
        bulk.pull_followers(db_conn, run=run, socialdata_fetcher=fetcher)
    )
    assert len(profiles) == 1
    assert profiles[0]["screen_name"] == "user2"
    final = bulk.get_run(db_conn, run.run_id)
    assert final.rows_inserted == 1


def test_pull_followers_marks_partial_on_exception(db_conn):
    run = bulk.create_run(
        db_conn,
        target_handle="doji_com",
        target_user_id="555",
        extract_type="followers",
    )

    def fetcher(path, params):
        if "cursor" in params:
            raise RuntimeError("HTTP 429 rate limited")
        return _page([_make_user(1)], cursor="next")

    with pytest.raises(RuntimeError):
        list(bulk.pull_followers(db_conn, run=run, socialdata_fetcher=fetcher))

    final = bulk.get_run(db_conn, run.run_id)
    assert final.cursor_completed == 0
    assert final.partial_failure_reason == "429_rate_limit"
    # First page checkpoint should still have advanced the cursor.
    assert final.last_cursor == "next"
    assert final.pages_fetched == 1


def test_pull_followers_resumes_from_last_cursor(db_conn):
    run = bulk.create_run(
        db_conn,
        target_handle="doji_com",
        target_user_id="555",
        extract_type="followers",
    )
    # Pretend a prior partial run got us through cursor_a.
    db_conn.execute(
        "UPDATE kol_extract_runs SET last_cursor='cursor_a', pages_fetched=1 "
        "WHERE run_id = :r",
        {"r": run.run_id},
    )
    db_conn.commit()
    resumed = bulk.get_run(db_conn, run.run_id)

    seen: list[dict] = []

    def fetcher(path, params):
        seen.append(dict(params))
        return _page([_make_user(99)], cursor=None)

    list(bulk.pull_followers(db_conn, run=resumed, socialdata_fetcher=fetcher))
    # First (and only) call carries the resume cursor.
    assert seen[0]["cursor"] == "cursor_a"


def test_pull_following_skips_when_expected_exceeds_max(db_conn):
    run = bulk.create_run(
        db_conn,
        target_handle="bigbrain",
        target_user_id="777",
        extract_type="following",
        expected_count=5000,
    )

    def fetcher(path, params):
        raise AssertionError("should not be called when expected_count > max_following")

    profiles = list(
        bulk.pull_following(
            db_conn, run=run, max_following=1000, socialdata_fetcher=fetcher
        )
    )
    assert profiles == []
    final = bulk.get_run(db_conn, run.run_id)
    assert final.cursor_completed == 1


def test_pull_following_does_not_floor_filter(db_conn):
    """Following entries should NOT be dropped by follower-count floor."""
    run = bulk.create_run(
        db_conn,
        target_handle="curator",
        target_user_id="888",
        extract_type="following",
    )
    pages = [_page([
        _make_user(1, followers_count=50),
        _make_user(2, followers_count=200),
    ], cursor=None)]

    def fetcher(path, params):
        return pages.pop(0)

    profiles = list(
        bulk.pull_following(db_conn, run=run, socialdata_fetcher=fetcher)
    )
    assert len(profiles) == 2
