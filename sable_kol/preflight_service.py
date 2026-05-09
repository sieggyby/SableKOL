"""SableKOL preflight FastAPI sidecar.

Three endpoints, all gated by the ``X-Sable-Service-Token`` header:

* ``POST /preflight`` — looks up a handle on X (xAI Grok live), returns
  enrichment + axis candidates + comparable projects + signal_metadata.
* ``POST /suggest-comparable`` — re-runs only the comparable-projects
  suggestion (used when the operator changes themes mid-wizard).
* ``POST /reuse-check`` — DB-only query against ``kol_extract_runs`` to
  split a candidate cohort list into ``already_have`` / ``must_fetch`` plus
  an estimated SocialData spend. No xAI call, no spend.

Health probe at ``GET /healthz`` (token-free) for the compose healthcheck.

Deployment: this module is the entry point baked into ``Dockerfile.preflight``
and run via uvicorn. ``XAI_API_KEY`` and ``SABLE_SERVICE_TOKEN`` MUST be set
in the container's environment — the service hard-fails on missing token at
request time. The container is reachable only over the compose network; no
``ports:`` block is published.
"""
from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

from fastapi import FastAPI, Header, HTTPException, status
from fastapi.responses import JSONResponse

from sable_kol.db import normalize_handle
from sable_kol.grok_api import (
    GrokAPIError,
    GrokAuthError,
    GrokParseError,
    build_preflight_response,
    build_suggest_comparable_response,
)
from sable_kol.preflight_schemas import (
    PreflightRequest,
    PreflightResponse,
    ReuseCheckRequest,
    ReuseCheckResponse,
    SuggestComparableRequest,
    SuggestComparableResponse,
)


logger = logging.getLogger(__name__)


# Conservative per-cohort SocialData estimate. Empirically the SolStitch
# follower extracts at $0.002/page understated by ~3x (see memory file
# ``feedback_cost_estimate_framing.md``). $4.50/cohort is the rounded
# 3x-multiplied estimate; this is the wizard's pre-submit projection only.
# The actual spend is logged via ``cost_events`` per the worker.
COST_USD_PER_COHORT_FETCH = 4.50


app = FastAPI(
    title="SableKOL Preflight",
    version="1",
    docs_url=None,           # No public Swagger UI — the sidecar is internal-only.
    redoc_url=None,
    openapi_url=None,
)


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------


def _require_service_token(token: str | None) -> None:
    expected = os.environ.get("SABLE_SERVICE_TOKEN")
    if not expected:
        # Hard-fail: the container should never be running without this set.
        # We don't fall back to "allow all" — that would be a worst-case
        # silent-default of the kind hard-fail-on-missing was meant to prevent.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="SABLE_SERVICE_TOKEN not configured on the sidecar",
        )
    if not token or not secrets.compare_digest(token, expected):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="invalid or missing service token",
        )


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@app.get("/healthz")
def healthz() -> dict[str, str]:
    """Token-free liveness probe for the compose healthcheck."""
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# /preflight
# ---------------------------------------------------------------------------


@app.post("/preflight", response_model=PreflightResponse)
def preflight(
    body: PreflightRequest,
    x_sable_service_token: Annotated[str | None, Header()] = None,
) -> PreflightResponse:
    _require_service_token(x_sable_service_token)
    try:
        return build_preflight_response(body.handle)
    except GrokAuthError as e:
        logger.error("xAI auth failure on /preflight: %s", e)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="xAI auth failure — operator must fill manually",
        ) from e
    except GrokParseError as e:
        logger.warning("xAI parse failure on /preflight: %s", e)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"xAI returned an unparseable response: {e}",
        ) from e
    except GrokAPIError as e:
        logger.warning("xAI request failure on /preflight: %s", e)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"xAI request failed: {e}",
        ) from e


# ---------------------------------------------------------------------------
# /suggest-comparable
# ---------------------------------------------------------------------------


@app.post("/suggest-comparable", response_model=SuggestComparableResponse)
def suggest_comparable(
    body: SuggestComparableRequest,
    x_sable_service_token: Annotated[str | None, Header()] = None,
) -> SuggestComparableResponse:
    _require_service_token(x_sable_service_token)
    try:
        return build_suggest_comparable_response(body.handle, body.themes)
    except GrokAuthError as e:
        logger.error("xAI auth failure on /suggest-comparable: %s", e)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="xAI auth failure",
        ) from e
    except GrokParseError as e:
        logger.warning("xAI parse failure on /suggest-comparable: %s", e)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"xAI returned an unparseable response: {e}",
        ) from e
    except GrokAPIError as e:
        logger.warning("xAI request failure on /suggest-comparable: %s", e)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"xAI request failed: {e}",
        ) from e


# ---------------------------------------------------------------------------
# /reuse-check
# ---------------------------------------------------------------------------


def cohorts_to_fetch(
    db: Any,
    comparison_handles: list[str],
    freshness_days: int = 180,
) -> tuple[list[str], list[str]]:
    """Split a candidate cohort list into (already_have, must_fetch).

    Dual-driver: uses ``?`` positional placeholders + ISO-8601 string
    comparison so the same query runs on SQLite (dev / tests) and Postgres
    (prod). CompatConnection translates ``?`` positional to SQLAlchemy
    named params for Postgres.

    A cohort is considered already-fetched when ``kol_extract_runs`` has at
    least one row for the normalized handle with ``extract_type='followers'``,
    ``cursor_completed=1``, and ``completed_at`` newer than the cutoff.
    """
    norm = [normalize_handle(h) for h in comparison_handles]
    if not norm:
        return [], []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=freshness_days)).isoformat()
    placeholders = ",".join("?" * len(norm))
    sql = f"""
        SELECT DISTINCT target_handle_normalized
        FROM kol_extract_runs
        WHERE target_handle_normalized IN ({placeholders})
          AND extract_type = 'followers'
          AND cursor_completed = 1
          AND completed_at > ?
    """
    rows = db.execute(sql, (*norm, cutoff)).fetchall()
    already_have_set = {r[0] for r in rows}
    already_have = [h for h in norm if h in already_have_set]
    must_fetch = [h for h in norm if h not in already_have_set]
    return already_have, must_fetch


@app.post("/reuse-check", response_model=ReuseCheckResponse)
def reuse_check(
    body: ReuseCheckRequest,
    x_sable_service_token: Annotated[str | None, Header()] = None,
) -> ReuseCheckResponse:
    _require_service_token(x_sable_service_token)
    from sable_kol.db import open_db

    with open_db() as conn:
        already_have, must_fetch = cohorts_to_fetch(
            conn, body.handles, body.freshness_days
        )
    return ReuseCheckResponse(
        already_have=already_have,
        must_fetch=must_fetch,
        estimated_cost_usd=round(len(must_fetch) * COST_USD_PER_COHORT_FETCH, 2),
        freshness_days=body.freshness_days,
    )


# ---------------------------------------------------------------------------
# Generic error handler — never leak xAI keys or stack traces in detail.
# ---------------------------------------------------------------------------


@app.exception_handler(Exception)
def _unhandled_exception_handler(request, exc: Exception) -> JSONResponse:  # noqa: ARG001
    logger.exception("unhandled error in preflight service")
    return JSONResponse(
        status_code=500,
        content={"detail": "internal error"},
    )
