"""Tests for sable_kol.preflight_service — the FastAPI sidecar.

Service-token rejection, /reuse-check happy path with seeded
``kol_extract_runs``, /preflight happy path with mocked Grok client.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest


fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from sable_kol.preflight_schemas import (  # noqa: E402
    ComparableProject,
    PreflightResponse,
    SignalMetadata,
)


TEST_TOKEN = "test-service-token-abc123"


@pytest.fixture
def app_client(monkeypatch):
    monkeypatch.setenv("SABLE_SERVICE_TOKEN", TEST_TOKEN)
    from sable_kol.preflight_service import app
    return TestClient(app)


@pytest.fixture
def threaded_db_conn():
    """In-memory SQLite CompatConnection that survives the TestClient thread hop.

    The standard ``db_conn`` fixture in conftest.py is fine for direct test
    code, but FastAPI's TestClient runs requests in a worker thread — the
    sqlite3 module rejects cross-thread connection use unless built with
    ``check_same_thread=False``. StaticPool keeps a single connection alive
    so the in-memory schema is reachable across threads.
    """
    import sqlite3

    from sqlalchemy import create_engine, event
    from sqlalchemy.pool import StaticPool

    from sable_platform.db.compat_conn import CompatConnection
    from sable_platform.db.connection import ensure_schema

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_conn, connection_record):  # noqa: ARG001
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    raw_proxy = engine.raw_connection()
    try:
        dbapi_conn = raw_proxy.dbapi_connection
        dbapi_conn.row_factory = sqlite3.Row
        ensure_schema(dbapi_conn)
    finally:
        raw_proxy.close()

    sa_conn = engine.connect()
    conn = CompatConnection(sa_conn)
    yield conn
    conn.close()
    engine.dispose()


# ---------------------------------------------------------------------------
# Health probe
# ---------------------------------------------------------------------------


def test_healthz_no_token_required(app_client):
    r = app_client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# Service-token gate (every endpoint)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("endpoint,body", [
    ("/preflight", {"handle": "solstitch"}),
    ("/suggest-comparable", {"handle": "solstitch", "themes": []}),
    ("/reuse-check", {"handles": ["a"], "freshness_days": 180}),
])
def test_missing_token_403(app_client, endpoint, body):
    r = app_client.post(endpoint, json=body)
    assert r.status_code == 403


@pytest.mark.parametrize("endpoint,body", [
    ("/preflight", {"handle": "solstitch"}),
    ("/suggest-comparable", {"handle": "solstitch", "themes": []}),
    ("/reuse-check", {"handles": ["a"], "freshness_days": 180}),
])
def test_wrong_token_403(app_client, endpoint, body):
    r = app_client.post(
        endpoint, json=body, headers={"X-Sable-Service-Token": "wrong"}
    )
    assert r.status_code == 403


def test_unconfigured_token_503(monkeypatch):
    """If SABLE_SERVICE_TOKEN is unset on the sidecar, every gated endpoint
    returns 503 — no silent allow-all fallback."""
    monkeypatch.delenv("SABLE_SERVICE_TOKEN", raising=False)
    from sable_kol.preflight_service import app
    client = TestClient(app)
    r = client.post(
        "/preflight",
        json={"handle": "solstitch"},
        headers={"X-Sable-Service-Token": "anything"},
    )
    assert r.status_code == 503


# ---------------------------------------------------------------------------
# /preflight — happy path with mocked Grok
# ---------------------------------------------------------------------------


def _fake_preflight_response(handle: str = "solstitch") -> PreflightResponse:
    return PreflightResponse(
        handle=handle,
        twitter_id="12345",
        bio="Tokenized fashion launchpad",
        followers=50000,
        verified=False,
        is_active=True,
        primary_archetype="founder",
        primary_sectors=["fashion"],
        credibility_signal="medium",
        real_name_known=False,
        listed_count=100,
        tweets_count=5000,
        following=500,
        notes=None,
        recent_themes=["fashion", "RWA"],
        audience_archetype="fashion-leaning crypto natives",
        axis_candidates=[],
        comparable_projects=[
            ComparableProject(handle="metafactory", rationale="rwa", shared_themes=["RWA"]),
        ],
        signal_metadata=SignalMetadata(
            source="grok_xai_live",
            model="grok-4-latest",
            fetched_at_utc="2026-05-09T15:00:00Z",
            signal_type="interpretive",
        ),
    )


def test_preflight_happy(app_client, monkeypatch):
    monkeypatch.setattr(
        "sable_kol.preflight_service.build_preflight_response",
        lambda h, **_kw: _fake_preflight_response(h.lstrip("@").lower()),
    )
    r = app_client.post(
        "/preflight",
        json={"handle": "@SolStitch"},
        headers={"X-Sable-Service-Token": TEST_TOKEN},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["handle"] == "solstitch"
    assert body["signal_metadata"]["source"] == "grok_xai_live"
    assert body["signal_metadata"]["signal_type"] == "interpretive"
    assert len(body["comparable_projects"]) == 1


def test_preflight_xai_auth_failure_returns_503(app_client, monkeypatch):
    from sable_kol.grok_api import GrokAuthError

    def raise_auth(_h, **_kw):
        raise GrokAuthError("xAI rejected the key")

    monkeypatch.setattr(
        "sable_kol.preflight_service.build_preflight_response", raise_auth
    )
    r = app_client.post(
        "/preflight",
        json={"handle": "solstitch"},
        headers={"X-Sable-Service-Token": TEST_TOKEN},
    )
    assert r.status_code == 503


def test_preflight_xai_parse_failure_returns_502(app_client, monkeypatch):
    from sable_kol.grok_api import GrokParseError

    def raise_parse(_h, **_kw):
        raise GrokParseError("schema drift")

    monkeypatch.setattr(
        "sable_kol.preflight_service.build_preflight_response", raise_parse
    )
    r = app_client.post(
        "/preflight",
        json={"handle": "solstitch"},
        headers={"X-Sable-Service-Token": TEST_TOKEN},
    )
    assert r.status_code == 502


# ---------------------------------------------------------------------------
# /reuse-check — DB-only, dual-driver query
# ---------------------------------------------------------------------------


def _seed_extract_run(
    threaded_db_conn,
    *,
    run_id: str,
    handle: str,
    completed_at: str,
    cursor_completed: int = 1,
    extract_type: str = "followers",
    client_id: str = "_external",
):
    threaded_db_conn.execute(
        "INSERT INTO kol_extract_runs "
        "  (run_id, target_handle_normalized, target_user_id, provider, "
        "   extract_type, cursor_completed, completed_at, client_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, handle, "1", "socialdata", extract_type,
         cursor_completed, completed_at, client_id),
    )
    threaded_db_conn.commit()


def _patch_open_db(monkeypatch, db_conn):
    @contextmanager
    def fake_open_db():
        yield db_conn

    monkeypatch.setattr("sable_kol.db.open_db", fake_open_db)


def test_reuse_check_splits_correctly(app_client, threaded_db_conn, monkeypatch):
    fresh = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    _seed_extract_run(threaded_db_conn, run_id="r1", handle="solstitch", completed_at=fresh)
    _patch_open_db(monkeypatch, threaded_db_conn)

    r = app_client.post(
        "/reuse-check",
        json={"handles": ["@SolStitch", "metafactory", "rtfkt"], "freshness_days": 180},
        headers={"X-Sable-Service-Token": TEST_TOKEN},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["already_have"] == ["solstitch"]
    assert sorted(body["must_fetch"]) == ["metafactory", "rtfkt"]
    assert body["estimated_cost_usd"] == round(2 * 1.00, 2)
    assert body["freshness_days"] == 180


def test_reuse_check_stale_falls_into_must_fetch(app_client, threaded_db_conn, monkeypatch):
    """Run completed 200 days ago is older than 180-day freshness — counts as
    must-fetch, not already-have."""
    stale = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
    _seed_extract_run(threaded_db_conn, run_id="r1", handle="solstitch", completed_at=stale)
    _patch_open_db(monkeypatch, threaded_db_conn)

    r = app_client.post(
        "/reuse-check",
        json={"handles": ["solstitch"], "freshness_days": 180},
        headers={"X-Sable-Service-Token": TEST_TOKEN},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["already_have"] == []
    assert body["must_fetch"] == ["solstitch"]


def test_reuse_check_partial_run_excluded(app_client, threaded_db_conn, monkeypatch):
    """cursor_completed=0 means the extract was interrupted — its data is
    contaminated. Reuse-check should NOT count it as already-have."""
    fresh = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    _seed_extract_run(
        threaded_db_conn, run_id="r1", handle="solstitch",
        completed_at=fresh, cursor_completed=0,
    )
    _patch_open_db(monkeypatch, threaded_db_conn)

    r = app_client.post(
        "/reuse-check",
        json={"handles": ["solstitch"], "freshness_days": 180},
        headers={"X-Sable-Service-Token": TEST_TOKEN},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["already_have"] == []
    assert body["must_fetch"] == ["solstitch"]


def test_reuse_check_wrong_extract_type_excluded(app_client, threaded_db_conn, monkeypatch):
    """A 'following' extract isn't usable for follower-cohort surveys."""
    fresh = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    _seed_extract_run(
        threaded_db_conn, run_id="r1", handle="solstitch",
        completed_at=fresh, extract_type="following",
    )
    _patch_open_db(monkeypatch, threaded_db_conn)

    r = app_client.post(
        "/reuse-check",
        json={"handles": ["solstitch"], "freshness_days": 180},
        headers={"X-Sable-Service-Token": TEST_TOKEN},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["already_have"] == []


def test_reuse_check_empty_handles(app_client, monkeypatch):
    """Empty input should short-circuit before any DB query."""
    # No DB patch — the contextmanager should never be entered.
    sentinel = {"called": False}

    @contextmanager
    def boom():
        sentinel["called"] = True
        yield None

    monkeypatch.setattr("sable_kol.db.open_db", boom)

    r = app_client.post(
        "/reuse-check",
        json={"handles": [], "freshness_days": 180},
        headers={"X-Sable-Service-Token": TEST_TOKEN},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["already_have"] == []
    assert body["must_fetch"] == []
    assert body["estimated_cost_usd"] == 0.0


def test_reuse_check_normalizes_handles(app_client, threaded_db_conn, monkeypatch):
    fresh = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    _seed_extract_run(threaded_db_conn, run_id="r1", handle="solstitch", completed_at=fresh)
    _patch_open_db(monkeypatch, threaded_db_conn)

    r = app_client.post(
        "/reuse-check",
        json={"handles": ["@SOLSTITCH ", "@MetaFactory"], "freshness_days": 180},
        headers={"X-Sable-Service-Token": TEST_TOKEN},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["already_have"] == ["solstitch"]
    assert body["must_fetch"] == ["metafactory"]


# ---------------------------------------------------------------------------
# KO-1.b — sidecar passthrough for preflight context flags
# ---------------------------------------------------------------------------


def test_preflight_forwards_context_and_exclude_handles(app_client, monkeypatch):
    """Operator priming on the request body must reach build_preflight_response."""
    captured = {}

    def fake_build(handle, **kwargs):
        captured["handle"] = handle
        captured.update(kwargs)
        return _fake_preflight_response(handle.lstrip("@").lower())

    monkeypatch.setattr(
        "sable_kol.preflight_service.build_preflight_response", fake_build
    )
    r = app_client.post(
        "/preflight",
        json={
            "handle": "tigfoundation",
            "context": "TIG is DeAI on Base",
            "exclude_handles": ["solstitch", "multisynq"],
            "allow_non_crypto_research": True,
        },
        headers={"X-Sable-Service-Token": TEST_TOKEN},
    )
    assert r.status_code == 200
    assert captured["context"] == "TIG is DeAI on Base"
    assert captured["exclude_handles"] == ["solstitch", "multisynq"]
    assert captured["allow_non_crypto_research"] is True


def test_preflight_priming_defaults_when_omitted(app_client, monkeypatch):
    """Older clients can omit the priming fields; defaults match prior behavior."""
    captured = {}

    def fake_build(handle, **kwargs):
        captured.update(kwargs)
        return _fake_preflight_response(handle)

    monkeypatch.setattr(
        "sable_kol.preflight_service.build_preflight_response", fake_build
    )
    r = app_client.post(
        "/preflight",
        json={"handle": "solstitch"},
        headers={"X-Sable-Service-Token": TEST_TOKEN},
    )
    assert r.status_code == 200
    assert captured["context"] is None
    assert captured["exclude_handles"] is None
    assert captured["allow_non_crypto_research"] is False


def test_suggest_comparable_forwards_priming_flags(app_client, monkeypatch):
    """Same passthrough on /suggest-comparable so theme-edit re-runs keep priming."""
    from sable_kol.preflight_schemas import (
        ComparableProject,
        SignalMetadata,
        SuggestComparableResponse,
    )

    captured = {}

    def fake_build(handle, themes, **kwargs):
        captured["handle"] = handle
        captured["themes"] = themes
        captured.update(kwargs)
        return SuggestComparableResponse(
            source_handle=handle,
            comparable_projects=[
                ComparableProject(handle="metafactory", rationale="rwa", shared_themes=[]),
            ],
            signal_metadata=SignalMetadata(
                source="grok_xai_live",
                model="grok-4-latest",
                fetched_at_utc="2026-05-10T15:00:00Z",
                signal_type="interpretive",
            ),
        )

    monkeypatch.setattr(
        "sable_kol.preflight_service.build_suggest_comparable_response", fake_build
    )
    r = app_client.post(
        "/suggest-comparable",
        json={
            "handle": "tigfoundation",
            "themes": ["math", "ai-ml"],
            "context": "TIG context",
            "exclude_handles": ["solstitch"],
            "allow_non_crypto_research": True,
        },
        headers={"X-Sable-Service-Token": TEST_TOKEN},
    )
    assert r.status_code == 200
    assert captured["themes"] == ["math", "ai-ml"]
    assert captured["context"] == "TIG context"
    assert captured["exclude_handles"] == ["solstitch"]
    assert captured["allow_non_crypto_research"] is True


# ---------------------------------------------------------------------------
# /draft-intro (KO-3) — sidecar surface
# ---------------------------------------------------------------------------


def _fake_draft(handle: str = "alice") -> "ColdIntroDraft":
    from sable_kol.preflight_schemas import ColdIntroDraft, SignalMetadata

    return ColdIntroDraft(
        intro_text="saw your AlphaEvolve note — sharp.\nbuilding TIG, worth a 5-min sync?",
        suggested_angle="leans on top_signals: AlphaEvolve commentary",
        signal_metadata=SignalMetadata(
            source="grok_xai_live",
            model="grok-4-latest",
            fetched_at_utc="2026-05-10T15:00:00Z",
            signal_type="interpretive",
            caveat="ai-drafted",
        ),
    )


@pytest.mark.parametrize("token,expected", [
    (None, 403),
    ("wrong", 403),
])
def test_draft_intro_token_gate(monkeypatch, token, expected):
    monkeypatch.setenv("SABLE_SERVICE_TOKEN", TEST_TOKEN)
    from sable_kol.preflight_service import app
    client = TestClient(app)
    headers = {} if token is None else {"X-Sable-Service-Token": token}
    r = client.post(
        "/draft-intro",
        json={
            "handle": "alice",
            "persona": "sieggy",
            "project_context": "",
            "candidate_signal": {"handle": "alice"},
        },
        headers=headers,
    )
    assert r.status_code == expected


def test_draft_intro_unconfigured_token_503(monkeypatch):
    monkeypatch.delenv("SABLE_SERVICE_TOKEN", raising=False)
    from sable_kol.preflight_service import app
    client = TestClient(app)
    r = client.post(
        "/draft-intro",
        json={
            "handle": "alice",
            "persona": "sieggy",
            "project_context": "",
            "candidate_signal": {"handle": "alice"},
        },
        headers={"X-Sable-Service-Token": "anything"},
    )
    assert r.status_code == 503


def test_draft_intro_happy(app_client, monkeypatch):
    captured = {}

    def fake_draft(*, handle, persona, project_context, candidate_signal):
        captured["handle"] = handle
        captured["persona"] = persona
        captured["project_context"] = project_context
        captured["candidate_signal"] = candidate_signal
        return _fake_draft(handle)

    monkeypatch.setattr(
        "sable_kol.preflight_service.draft_cold_intro", fake_draft
    )
    r = app_client.post(
        "/draft-intro",
        json={
            "handle": "alice",
            "persona": "sparta",
            "project_context": "TIG outreach",
            "candidate_signal": {
                "handle": "alice",
                "bio_snapshot": "convex optimization",
                "top_signals": ["AlphaEvolve commentary"],
                "tier": "B",
            },
        },
        headers={"X-Sable-Service-Token": TEST_TOKEN},
    )
    assert r.status_code == 200
    body = r.json()
    assert "intro_text" in body
    assert body["signal_metadata"]["source"] == "grok_xai_live"
    assert captured["persona"] == "sparta"
    assert captured["candidate_signal"].handle == "alice"
    assert captured["candidate_signal"].top_signals == ["AlphaEvolve commentary"]


def test_draft_intro_invalid_persona_422(app_client):
    """Pydantic Literal violation surfaces as 422 — bad persona slug is bounced."""
    r = app_client.post(
        "/draft-intro",
        json={
            "handle": "alice",
            "persona": "shitposter",  # not a real slug
            "project_context": "",
            "candidate_signal": {"handle": "alice"},
        },
        headers={"X-Sable-Service-Token": TEST_TOKEN},
    )
    assert r.status_code == 422


def test_draft_intro_ben_returns_409(app_client, monkeypatch):
    """Ben drafts are placeholder-blocked with structured 409 detail."""
    called = []

    def fake_draft(**_kw):
        called.append(1)
        from sable_kol.grok_api import GrokPersonaPlaceholderError
        raise GrokPersonaPlaceholderError("ben placeholder")

    monkeypatch.setattr(
        "sable_kol.preflight_service.draft_cold_intro", fake_draft
    )
    r = app_client.post(
        "/draft-intro",
        json={
            "handle": "alice",
            "persona": "ben",
            "project_context": "",
            "candidate_signal": {"handle": "alice"},
        },
        headers={"X-Sable-Service-Token": TEST_TOKEN},
    )
    assert r.status_code == 409
    body = r.json()
    assert body["detail"]["error"] == "persona_placeholder"
    assert body["detail"]["persona"] == "ben"


def test_draft_intro_extra_signal_field_422(app_client):
    """CandidateIntroSignal is extra='forbid' — unknown keys 422 before xAI."""
    r = app_client.post(
        "/draft-intro",
        json={
            "handle": "alice",
            "persona": "sieggy",
            "project_context": "",
            "candidate_signal": {
                "handle": "alice",
                "relationship_notes": "DO NOT LEAK ME",  # not whitelisted
            },
        },
        headers={"X-Sable-Service-Token": TEST_TOKEN},
    )
    assert r.status_code == 422


def test_draft_intro_xai_failure_502(app_client, monkeypatch):
    from sable_kol.grok_api import GrokAPIError

    def fake_draft(**_kw):
        raise GrokAPIError("xAI 500")

    monkeypatch.setattr(
        "sable_kol.preflight_service.draft_cold_intro", fake_draft
    )
    r = app_client.post(
        "/draft-intro",
        json={
            "handle": "alice",
            "persona": "sieggy",
            "project_context": "",
            "candidate_signal": {"handle": "alice"},
        },
        headers={"X-Sable-Service-Token": TEST_TOKEN},
    )
    assert r.status_code == 502
