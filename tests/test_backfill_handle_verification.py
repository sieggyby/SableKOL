"""Tests for scripts/backfill_handle_verification.py (KO-5).

Covers the classify-and-archive logic. SocialData HTTP is mocked via
httpx.MockTransport so the test suite never makes real network calls
or burns SocialData credit. The script's CLI surface (argparse + I/O)
is exercised by the production smoke; these tests target the units.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import httpx
import pytest


def _load_script():
    """Import scripts/backfill_handle_verification.py as a module.

    Must register in sys.modules BEFORE exec_module — Python 3.14's
    dataclass machinery resolves field annotations via
    ``sys.modules[cls.__module__]`` and crashes with AttributeError if
    the module isn't reachable that way.
    """
    import sys
    name = "scripts_backfill_handle_verification"
    if name in sys.modules:
        return sys.modules[name]
    path = Path(__file__).parent.parent / "scripts" / "backfill_handle_verification.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# classify() — the per-handle verdict function
# ---------------------------------------------------------------------------


def _patch_http_client(monkeypatch, handler):
    """Swap httpx.Client globally so the SocialData call routes through the
    handler. Binds the real Client class to a local before patching so the
    factory doesn't recurse through the monkeypatched name."""
    real_client_cls = httpx.Client

    def factory(**_kw):
        return real_client_cls(transport=httpx.MockTransport(handler))

    monkeypatch.setattr("httpx.Client", factory)


def test_classify_alive_returns_alive_verdict(monkeypatch):
    monkeypatch.setenv("SOCIALDATA_API_KEY", "sd-test")
    bk = _load_script()
    _patch_http_client(
        monkeypatch,
        lambda req: httpx.Response(
            200, json={"id_str": "42", "screen_name": "alice", "description": "bio"}
        ),
    )
    result = bk.classify("alice")
    assert result.verdict == bk.VERDICT_ALIVE
    assert result.handle == "alice"


def test_classify_404_returns_not_found(monkeypatch):
    monkeypatch.setenv("SOCIALDATA_API_KEY", "sd-test")
    monkeypatch.setattr("time.sleep", lambda _s: None)
    bk = _load_script()
    _patch_http_client(monkeypatch, lambda req: httpx.Response(404, text="not found"))
    result = bk.classify("ghost")
    assert result.verdict == bk.VERDICT_NOT_FOUND
    assert "404" in result.detail


def test_classify_200_with_status_error_returns_not_found(monkeypatch):
    """SocialData sometimes 200s with {"status":"error","message":"User not found"}."""
    monkeypatch.setenv("SOCIALDATA_API_KEY", "sd-test")
    bk = _load_script()
    _patch_http_client(
        monkeypatch,
        lambda req: httpx.Response(
            200, json={"status": "error", "message": "User not found"}
        ),
    )
    result = bk.classify("ghost2")
    assert result.verdict == bk.VERDICT_NOT_FOUND


def test_classify_200_with_suspended_message_returns_suspended(monkeypatch):
    """Distinct verdict for suspended accounts — operators may want to keep
    suspended-but-not-deleted handles for re-check later."""
    monkeypatch.setenv("SOCIALDATA_API_KEY", "sd-test")
    bk = _load_script()
    _patch_http_client(
        monkeypatch,
        lambda req: httpx.Response(
            200, json={"status": "error", "message": "User is suspended"}
        ),
    )
    result = bk.classify("suspendme")
    assert result.verdict == bk.VERDICT_SUSPENDED
    assert "suspend" in result.detail.lower()


def test_classify_5xx_after_retries_returns_error_not_archive(monkeypatch):
    """A 5xx after retries → ERROR (fail open). We must NOT mistake
    transient SocialData weather for a hallucinated handle, or we'd
    archive real candidates."""
    monkeypatch.setenv("SOCIALDATA_API_KEY", "sd-test")
    monkeypatch.setattr("time.sleep", lambda _s: None)
    bk = _load_script()
    _patch_http_client(monkeypatch, lambda req: httpx.Response(503, text="down"))
    result = bk.classify("realhandle")
    assert result.verdict == bk.VERDICT_ERROR


def test_classify_propagates_balance_exhausted(monkeypatch):
    """402 must propagate so the caller can abort the batch — no point
    burning further retries when the account is empty."""
    monkeypatch.setenv("SOCIALDATA_API_KEY", "sd-test")
    bk = _load_script()
    _patch_http_client(monkeypatch, lambda req: httpx.Response(402, text="balance"))
    with pytest.raises(bk.BalanceExhaustedError):
        bk.classify("any")


# ---------------------------------------------------------------------------
# select_candidates() — filter SQL semantics
# ---------------------------------------------------------------------------


def test_select_candidates_risky_filter_targets_no_bio_no_twitter_id(db_conn):
    """The 'risky' filter is the highest-confidence hallucination signal:
    no bio AND no twitter_id."""
    bk = _load_script()
    # Seed three rows: one risky, one with bio (safe), one with twitter_id (safe)
    db_conn.execute(
        "INSERT INTO kol_candidates "
        "(handle_normalized, bio_snapshot, twitter_id, is_unresolved, status) "
        "VALUES (:h, :bio, :tid, 0, 'active')",
        {"h": "risky_one", "bio": None, "tid": None},
    )
    db_conn.execute(
        "INSERT INTO kol_candidates "
        "(handle_normalized, bio_snapshot, twitter_id, is_unresolved, status) "
        "VALUES (:h, :bio, :tid, 0, 'active')",
        {"h": "has_bio", "bio": "real bio", "tid": None},
    )
    db_conn.execute(
        "INSERT INTO kol_candidates "
        "(handle_normalized, bio_snapshot, twitter_id, is_unresolved, status) "
        "VALUES (:h, :bio, :tid, 0, 'active')",
        {"h": "has_tid", "bio": None, "tid": "12345"},
    )
    db_conn.commit()

    risky = bk.select_candidates(db_conn, "risky", None)
    handles = [h for _, h in risky]
    assert "risky_one" in handles
    assert "has_bio" not in handles
    assert "has_tid" not in handles


def test_select_candidates_unverified_filter_targets_all_null_twitter_id(db_conn):
    """'unverified' = all rows missing twitter_id (broader than 'risky')."""
    bk = _load_script()
    db_conn.execute(
        "INSERT INTO kol_candidates "
        "(handle_normalized, bio_snapshot, twitter_id, is_unresolved, status) "
        "VALUES ('with_bio_no_tid', 'real bio', NULL, 0, 'active'), "
        "       ('with_tid', 'bio', '12345', 0, 'active')",
    )
    db_conn.commit()

    unv = bk.select_candidates(db_conn, "unverified", None)
    handles = [h for _, h in unv]
    assert "with_bio_no_tid" in handles
    assert "with_tid" not in handles


def test_select_candidates_skips_unresolved_and_archived(db_conn):
    """All filters must skip is_unresolved=1 rows + status != 'active'."""
    bk = _load_script()
    db_conn.execute(
        "INSERT INTO kol_candidates "
        "(handle_normalized, bio_snapshot, twitter_id, is_unresolved, status) "
        "VALUES ('unresolved_h', NULL, NULL, 1, 'active'), "
        "       ('archived_h', NULL, NULL, 0, 'archived')",
    )
    db_conn.commit()

    for name in ("risky", "unverified", "all"):
        handles = [h for _, h in bk.select_candidates(db_conn, name, None)]
        assert "unresolved_h" not in handles
        assert "archived_h" not in handles


def test_select_candidates_respects_limit(db_conn):
    bk = _load_script()
    for i in range(5):
        db_conn.execute(
            "INSERT INTO kol_candidates "
            "(handle_normalized, bio_snapshot, twitter_id, is_unresolved, status) "
            "VALUES (:h, NULL, NULL, 0, 'active')",
            {"h": f"risky{i}"},
        )
    db_conn.commit()
    selected = bk.select_candidates(db_conn, "risky", 3)
    assert len(selected) == 3


# ---------------------------------------------------------------------------
# archive_candidate() — write-side semantics
# ---------------------------------------------------------------------------


def test_archive_flips_status_and_appends_audit_source_tag(db_conn):
    bk = _load_script()
    db_conn.execute(
        "INSERT INTO kol_candidates "
        "(handle_normalized, discovery_sources_json, is_unresolved, status) "
        "VALUES ('ghost', :sources, 0, 'active')",
        {"sources": json.dumps(["list:cahit:1234"])},
    )
    db_conn.commit()
    row = db_conn.execute(
        "SELECT candidate_id FROM kol_candidates WHERE handle_normalized='ghost'"
    ).fetchone()
    cid = row["candidate_id"]

    bk.archive_candidate(db_conn, cid, "not_found", "SocialData 404")
    db_conn.commit()

    row = db_conn.execute(
        "SELECT status, discovery_sources_json FROM kol_candidates WHERE candidate_id=:c",
        {"c": cid},
    ).fetchone()
    assert row["status"] == "archived"
    sources = json.loads(row["discovery_sources_json"])
    assert "list:cahit:1234" in sources  # original preserved
    ko5_tag = [s for s in sources if s.startswith("kol_graph:archived_by_ko5:")]
    assert len(ko5_tag) == 1
    assert "not_found" in ko5_tag[0]
