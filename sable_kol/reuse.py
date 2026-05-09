"""Reuse-detection helpers for the any-project KOL wizard.

Shared between:

* :mod:`sable_kol.preflight_service` — the FastAPI sidecar's ``/reuse-check``
  endpoint exposes :func:`cohorts_to_fetch` to the wizard's Step-3 live
  debounce, plus the cost projection.
* :mod:`sable_kol.jobs` — the worker's ``reuse_check`` step calls the same
  function so the worker and the operator-facing UI agree on which cohorts
  are reused vs surveyed.

Why a separate module: Phase B shipped ``cohorts_to_fetch`` inside
``preflight_service.py`` to keep the sidecar self-contained. Phase C lifts
it out so the worker (which runs as a host process, NOT inside the sidecar
container) can import it without dragging FastAPI in. Both call sites
exercise the dual-driver SQL: ``?`` positional placeholders + ISO-8601
string comparison so the same query works on SQLite (dev/tests) and
Postgres (prod).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sable_kol.db import normalize_handle


# Conservative per-cohort SocialData estimate. Empirically the SolStitch
# follower extracts at $0.002/page understated by ~3x (see memory file
# ``feedback_cost_estimate_framing.md``). $4.50/cohort is the rounded
# 3x-multiplied estimate; this is the wizard's pre-submit projection only.
# The actual spend is logged via ``cost_events`` per the worker.
COST_USD_PER_COHORT_FETCH = 4.50


def cohorts_to_fetch(
    db: Any,
    comparison_handles: list[str],
    freshness_days: int = 180,
) -> tuple[list[str], list[str]]:
    """Split a candidate cohort list into ``(already_have, must_fetch)``.

    A cohort is considered already-fetched when ``kol_extract_runs`` has at
    least one row for the normalized handle with ``extract_type='followers'``,
    ``cursor_completed=1``, and ``completed_at`` newer than the cutoff.

    Dual-driver: ``?`` positional placeholders are translated to SQLAlchemy
    named params for Postgres by ``CompatConnection``. ISO-8601 string
    comparison on ``completed_at`` works on both SQLite and Postgres because
    the ordering is lex-correct on UTC-isoformat strings.
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


def estimate_fetch_cost_usd(must_fetch: list[str]) -> float:
    """Round-to-cents fixed-rate projection of the SocialData spend for fetching
    *must_fetch* cohorts. The actual spend is logged via ``cost_events`` per page;
    this is the wizard's pre-submit estimate only.
    """
    return round(len(must_fetch) * COST_USD_PER_COHORT_FETCH, 2)
