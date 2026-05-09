"""SableKOL worker for ``job_type='kol_create'`` jobs.

Driven by a 60s systemd timer (``deploy/jobs/sable-kol-jobs.timer``) which
fires ``sable-kol jobs run --job-type kol_create --max-jobs 1`` once per tick.
The worker:

  1. Claims one ``pending`` job (or stale-reclaims a crashed-worker's running
     job) via ``sable_platform.db.jobs.claim_next_job``.
  2. Walks the claimed job's ``job_steps`` rows in order, dispatching each
     pending step to a handler.
  3. Persists each step's output via ``complete_step`` so resumes are
     idempotent — a completed step is skipped on the next tick.
  4. Honors ``next_retry_at``: if a step is deferred (e.g. xAI 429 backoff),
     the worker releases the job back to ``pending`` and exits; the next
     timer tick re-claims it.
  5. On success across all steps, marks the job ``done``. On a step that
     exhausts its retry budget, marks the job ``failed``.

Step machine (per ``docs/any_project_wizard_plan.md``):

    enrich              retries=3   xAI Grok enrich_handle
    suggest_comparable  retries=3   xAI Grok suggest_comparable_projects
    reuse_check         retries=0   DB-only via sable_kol.reuse.cohorts_to_fetch
    survey_cohort_<h>   retries=2   SocialData bulk follower fetch (one step per handle)
    write_yaml          retries=0   write client YAML to PROD/LOCAL_CLIENT_DIR
    regenerate          retries=1   sable_kol.regenerate.run_regenerate

Concurrency: SocialData rate limits make parallelism counterproductive, so
``--max-jobs 1`` is the contract — exactly one job claimed per tick.
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from sable_platform.db.jobs import (
    complete_job,
    complete_step,
    defer_step,
    fail_job,
    fail_step,
    get_resumable_steps,
    release_job,
    start_step,
    claim_next_job,
)


logger = logging.getLogger(__name__)


MAX_RETRIES: dict[str, int] = {
    "enrich": 3,
    "suggest_comparable": 3,
    "reuse_check": 0,
    "write_yaml": 0,
    "regenerate": 1,
    # survey_cohort_<handle> matched by prefix in _max_retries_for()
}


def _max_retries_for(step_name: str) -> int:
    if step_name.startswith("survey_cohort_"):
        return 2
    return MAX_RETRIES.get(step_name, 0)


# ---------------------------------------------------------------------------
# Step-handler protocol
# ---------------------------------------------------------------------------


class StepDeferred(Exception):
    """Raised by a handler to defer the step (e.g. xAI 429 backoff).

    The worker sets ``next_retry_at`` on the step, releases the job back to
    ``pending``, and exits the tick. The next timer tick re-claims the job
    and re-attempts the deferred step.
    """

    def __init__(self, retry_after_seconds: int = 60, reason: str | None = None):
        super().__init__(reason or f"deferred {retry_after_seconds}s")
        self.retry_after_seconds = retry_after_seconds


@dataclass
class StepContext:
    """Everything a step handler needs to run.

    Handlers stay pure functions over this context — easier to test, easier
    to swap out the SocialData/xAI plumbing for stubs.
    """

    conn: Any                    # SablePlatform CompatConnection
    job_id: str
    org_id: str
    job_config: dict             # parsed jobs.config_json
    step_id: int
    step_name: str
    step_input: dict             # parsed job_steps.input_json
    prior_outputs: dict[str, dict]  # other completed steps' output_json by step_name


StepHandler = Callable[[StepContext], dict]


# ---------------------------------------------------------------------------
# Default step handlers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_past(iso_string: str | None) -> bool:
    """Return True if *iso_string* is None or in the past (UTC)."""
    if not iso_string:
        return True
    try:
        dt = datetime.fromisoformat(iso_string)
    except ValueError:
        return True
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt <= datetime.now(timezone.utc)


def _handle_enrich(ctx: StepContext) -> dict:
    """Step handler — call xAI Grok enrich_handle for the wizard handle."""
    from sable_kol.grok_api import GrokAPIError, enrich_handle

    handle = ctx.job_config.get("handle") or ctx.step_input.get("handle")
    if not handle:
        raise ValueError("enrich step missing 'handle' in job config")
    try:
        enriched = enrich_handle(handle)
    except GrokAPIError as e:
        # 429 surfaces as GrokAPIError after 3 internal retries — defer.
        if "429" in str(e):
            raise StepDeferred(retry_after_seconds=300, reason=str(e)) from e
        raise
    return enriched.model_dump()


def _handle_suggest_comparable(ctx: StepContext) -> dict:
    """Step handler — call xAI Grok suggest_comparable_projects."""
    from sable_kol.grok_api import GrokAPIError, suggest_comparable_projects

    handle = ctx.job_config.get("handle") or ctx.step_input.get("handle")
    themes = ctx.step_input.get("themes")
    if themes is None:
        # Fall back to enrich step's recent_themes if available.
        enrich_out = ctx.prior_outputs.get("enrich") or {}
        themes = enrich_out.get("recent_themes") or ctx.job_config.get("themes") or []
    if not handle:
        raise ValueError("suggest_comparable step missing 'handle'")
    try:
        comparables = suggest_comparable_projects(handle, themes)
    except GrokAPIError as e:
        if "429" in str(e):
            raise StepDeferred(retry_after_seconds=300, reason=str(e)) from e
        raise
    return {"comparable_projects": [c.model_dump() for c in comparables]}


def _handle_reuse_check(ctx: StepContext) -> dict:
    """Step handler — DB-only reuse split. No spend, no retry budget."""
    from sable_kol.reuse import cohorts_to_fetch, estimate_fetch_cost_usd

    candidates = (
        ctx.step_input.get("comparison_handles")
        or ctx.job_config.get("comparison_handles")
        or []
    )
    freshness_days = int(
        ctx.step_input.get("freshness_days")
        or ctx.job_config.get("freshness_days")
        or 180
    )
    already_have, must_fetch = cohorts_to_fetch(ctx.conn, candidates, freshness_days)
    return {
        "already_have": already_have,
        "must_fetch": must_fetch,
        "estimated_cost_usd": estimate_fetch_cost_usd(must_fetch),
        "freshness_days": freshness_days,
    }


def _handle_survey_cohort(ctx: StepContext) -> dict:
    """Step handler — bulk SocialData follower extract for one cohort handle.

    Idempotent on resume: ``socialdata_bulk.create_run`` writes a
    ``kol_extract_runs`` row per attempt, but cohorts already in the cursor-
    completed state are skipped via the reuse_check step before we ever get
    here. Within a single run, the cursor-paginated fetch upserts via
    ``insert_edges`` with conflict-ignore semantics, so a mid-fetch crash
    leaves a partial-but-non-duplicate run record.
    """
    from sable_kol import socialdata_bulk as bulk
    from sable_kol.db import normalize_handle

    # step_name format: 'survey_cohort_<handle>'
    handle_raw = ctx.step_name.removeprefix("survey_cohort_")
    handle = normalize_handle(handle_raw)
    client_id = ctx.job_config.get("client_id") or ctx.org_id

    uid = bulk.resolve_user_id(ctx.conn, handle)
    if uid is None:
        raise RuntimeError(f"could not resolve user_id for @{handle}")

    run = bulk.create_run(
        ctx.conn,
        target_handle=handle,
        target_user_id=uid,
        extract_type="followers",
        client_id=client_id,
    )
    n = 0
    edge_batch: list[dict] = []
    try:
        for profile in bulk.pull_followers(
            ctx.conn,
            run=run,
            floor_followers=int(ctx.step_input.get("floor_followers") or 500),
            page_limit=ctx.step_input.get("page_limit"),
        ):
            n += 1
            edge_batch.append(
                {
                    "follower_id": profile.get("id_str") or str(profile["id"]),
                    "follower_handle": profile.get("screen_name"),
                    "followed_id": uid,
                    "followed_handle": handle,
                }
            )
            if len(edge_batch) >= 100:
                bulk.insert_edges(ctx.conn, run_id=run.run_id, edges=edge_batch)
                edge_batch.clear()
    finally:
        if edge_batch:
            bulk.insert_edges(ctx.conn, run_id=run.run_id, edges=edge_batch)

    final = bulk.get_run(ctx.conn, run.run_id)
    return {
        "run_id": run.run_id,
        "handle": handle,
        "profiles_kept": n,
        "pages_fetched": (final.pages_fetched if final else 0),
        "cost_usd_logged": (final.cost_usd_logged if final else 0.0),
        "cursor_completed": bool(final and final.cursor_completed),
    }


def _handle_write_yaml(ctx: StepContext) -> dict:
    """Step handler — write a minimal client YAML from job_config.

    The wizard UI (Phase D) is responsible for shaping ``job_config`` into a
    schema that satisfies ``client_config.load_client_config``. This handler
    transforms the wizard's submission into the on-disk YAML format and
    drops it at ``client_config.PROD_CLIENT_DIR/<client_id>.yaml`` (prod) or
    ``LOCAL_CLIENT_DIR`` (dev — auto-detected by which directory exists).
    """
    import yaml

    from sable_kol.client_config import (
        LOCAL_CLIENT_DIR,
        PROD_CLIENT_DIR,
        assert_client_id,
    )

    client_id = ctx.job_config.get("client_id")
    if not client_id:
        raise ValueError("write_yaml: job_config missing 'client_id'")
    assert_client_id(client_id)

    payload = _build_client_yaml(ctx.job_config, ctx.prior_outputs)

    out_dir = PROD_CLIENT_DIR if PROD_CLIENT_DIR.is_dir() else LOCAL_CLIENT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{client_id}.yaml"
    with open(out_path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(payload, fh, sort_keys=False)

    return {"yaml_path": str(out_path), "client_id": client_id}


def _build_client_yaml(job_config: dict, prior_outputs: dict[str, dict]) -> dict:
    """Translate wizard submission + prior step outputs into client YAML shape.

    Required keys per ``client_config.load_client_config``: client_id,
    display_name, mode, network_axes (with x.keywords + y.keywords).
    Audiences derive from the must_fetch cohorts (the surveyed ones).
    """
    client_id = job_config["client_id"]
    handle = job_config.get("handle") or client_id

    # network_axes — wizard supplies labels; if it didn't supply keywords,
    # bootstrap with the label as a single keyword. Operator can edit later.
    axes_raw = job_config.get("network_axes") or {}
    x_raw = axes_raw.get("x") or {}
    y_raw = axes_raw.get("y") or {}
    x_keywords = x_raw.get("keywords") or [x_raw.get("label", "x")]
    y_keywords = y_raw.get("keywords") or [y_raw.get("label", "y")]

    # Audiences — combine reuse_check.must_fetch + already_have so the YAML
    # has the full audience set even if we didn't survey them this run.
    reuse_out = prior_outputs.get("reuse_check") or {}
    audience_handles = list(
        dict.fromkeys(
            (reuse_out.get("already_have") or [])
            + (reuse_out.get("must_fetch") or [])
            + (job_config.get("comparison_handles") or [])
        )
    )

    return {
        "client_id": client_id,
        "display_name": job_config.get("display_name") or handle,
        "mode": job_config.get("mode") or "stealth",
        "debut_date": job_config.get("debut_date"),
        "sector_focus": job_config.get("sector_focus") or [],
        "themes": job_config.get("themes") or [],
        "audiences": [
            {"handle": h, "label": f"{h}_audience", "curator_weight": 1.0}
            for h in audience_handles
            if h
        ],
        "manual_pins": job_config.get("manual_pins") or [],
        "org_denylist_extras": [],
        "person_allowlist_extras": [],
        "celebrity_denylist_extras": [],
        "network_axes": {
            "x": {
                "label": x_raw.get("label", "x"),
                "keywords": x_keywords,
                "saturation": int(x_raw.get("saturation") or 4),
            },
            "y": {
                "label": y_raw.get("label", "y"),
                "keywords": y_keywords,
                "saturation": int(y_raw.get("saturation") or 4),
            },
        },
        "tier_thresholds": job_config.get("tier_thresholds") or {},
        "_wizard": {
            "submitted_by_email": job_config.get("submitted_by_email"),
            "submitted_at_utc": job_config.get("submitted_at_utc"),
            "wizard_version": job_config.get("wizard_version", "1"),
        },
    }


def _handle_regenerate(ctx: StepContext) -> dict:
    """Step handler — invoke ``run_regenerate`` for the client_id."""
    from dataclasses import asdict

    from sable_kol.regenerate import run_regenerate

    client_id = ctx.job_config.get("client_id")
    if not client_id:
        raise ValueError("regenerate: job_config missing 'client_id'")
    summary = run_regenerate(client_id)
    return asdict(summary)


# Default step → handler dispatch table. Tests can pass an override via
# ``run_one_tick(handlers=...)``.
DEFAULT_HANDLERS: dict[str, StepHandler] = {
    "enrich": _handle_enrich,
    "suggest_comparable": _handle_suggest_comparable,
    "reuse_check": _handle_reuse_check,
    "write_yaml": _handle_write_yaml,
    "regenerate": _handle_regenerate,
    # survey_cohort_<handle> resolved by prefix in _resolve_handler()
}


def _resolve_handler(
    step_name: str, handlers: dict[str, StepHandler]
) -> StepHandler | None:
    if step_name in handlers:
        return handlers[step_name]
    if step_name.startswith("survey_cohort_") and "survey_cohort" in handlers:
        return handlers["survey_cohort"]
    if step_name.startswith("survey_cohort_"):
        return _handle_survey_cohort
    return None


# ---------------------------------------------------------------------------
# Worker entry point
# ---------------------------------------------------------------------------


@dataclass
class TickResult:
    """One worker-tick outcome — what claim_next_job returned and how it ended."""

    claimed: bool
    job_id: str | None
    job_outcome: str | None     # 'done' | 'failed' | 'released' | None (no claim)
    steps_run: list[str] = None  # type: ignore[assignment]
    error: str | None = None

    def __post_init__(self) -> None:
        if self.steps_run is None:
            self.steps_run = []


def run_one_tick(
    conn: Any,
    *,
    job_type: str = "kol_create",
    worker_id: str | None = None,
    handlers: dict[str, StepHandler] | None = None,
    stale_after_minutes: int = 10,
) -> TickResult:
    """Run one worker tick: claim → walk steps → finalize.

    Used by the systemd timer entry point and by tests. Tests pass
    ``handlers`` to stub out xAI / SocialData / regenerate.

    Returns a :class:`TickResult` describing what happened.
    """
    handlers = {**DEFAULT_HANDLERS, **(handlers or {})}
    wid = worker_id or f"sable-kol-{uuid.uuid4().hex[:12]}"

    claim = claim_next_job(conn, job_type, wid, stale_after_minutes)
    if claim is None:
        return TickResult(claimed=False, job_id=None, job_outcome=None)

    job_id = claim["job_id"]
    org_id = claim["org_id"]
    job_config = claim["config_json"]
    logger.info("worker=%s claimed job_id=%s org_id=%s", wid, job_id, org_id)

    steps = get_resumable_steps(conn, job_id)
    prior_outputs: dict[str, dict] = {}

    # First pass — collect outputs from already-completed steps so later
    # handlers can reference them via ctx.prior_outputs.
    for s in steps:
        if s["status"] == "completed":
            try:
                prior_outputs[s["step_name"]] = json.loads(s["output_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                prior_outputs[s["step_name"]] = {}

    steps_run: list[str] = []
    for s in steps:
        step_name = s["step_name"]
        status = s["status"]

        if status == "completed":
            continue

        # Deferred step: if next_retry_at is in the future, release and exit.
        # The next timer tick re-claims and re-attempts when the timer fires
        # past the retry-at instant.
        if not _is_past(s["next_retry_at"]):
            logger.info(
                "worker=%s step=%s deferred until %s — releasing job",
                wid, step_name, s["next_retry_at"],
            )
            release_job(conn, job_id)
            return TickResult(
                claimed=True, job_id=job_id, job_outcome="released",
                steps_run=steps_run,
            )

        max_retries = _max_retries_for(step_name)
        if status == "failed" and s["retries"] >= max_retries:
            err = f"step '{step_name}' exhausted retries ({s['retries']}/{max_retries})"
            logger.error("worker=%s job=%s %s", wid, job_id, err)
            fail_job(conn, job_id, err)
            return TickResult(
                claimed=True, job_id=job_id, job_outcome="failed",
                steps_run=steps_run, error=err,
            )

        handler = _resolve_handler(step_name, handlers)
        if handler is None:
            err = f"no handler registered for step '{step_name}'"
            logger.error("worker=%s job=%s %s", wid, job_id, err)
            fail_job(conn, job_id, err)
            return TickResult(
                claimed=True, job_id=job_id, job_outcome="failed",
                steps_run=steps_run, error=err,
            )

        # Run the step.
        start_step(conn, s["step_id"])
        try:
            step_input = json.loads(s["input_json"] or "{}")
        except json.JSONDecodeError:
            step_input = {}

        ctx = StepContext(
            conn=conn,
            job_id=job_id,
            org_id=org_id,
            job_config=job_config,
            step_id=s["step_id"],
            step_name=step_name,
            step_input=step_input,
            prior_outputs=prior_outputs,
        )

        try:
            output = handler(ctx) or {}
        except StepDeferred as deferred:
            retry_at = (
                datetime.now(timezone.utc)
                + timedelta(seconds=deferred.retry_after_seconds)
            ).isoformat()
            defer_step(conn, s["step_id"], retry_at)
            # Mark the running step back to pending so claim_next_job sees
            # it as resumable on the next tick.
            conn.execute(
                "UPDATE job_steps SET status='pending' WHERE step_id=?",
                (s["step_id"],),
            )
            conn.commit()
            release_job(conn, job_id)
            logger.info(
                "worker=%s step=%s deferred for %ds (next_retry_at=%s)",
                wid, step_name, deferred.retry_after_seconds, retry_at,
            )
            return TickResult(
                claimed=True, job_id=job_id, job_outcome="released",
                steps_run=steps_run,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("worker=%s job=%s step=%s failed", wid, job_id, step_name)
            fail_step(conn, s["step_id"], error=str(e))
            new_retries = s["retries"] + 1
            if new_retries >= max_retries:
                err = f"step '{step_name}' exhausted retries ({new_retries}/{max_retries}): {e}"
                fail_job(conn, job_id, err)
                return TickResult(
                    claimed=True, job_id=job_id, job_outcome="failed",
                    steps_run=steps_run, error=err,
                )
            # Below the retry budget: release; next tick re-attempts.
            release_job(conn, job_id)
            return TickResult(
                claimed=True, job_id=job_id, job_outcome="released",
                steps_run=steps_run, error=str(e),
            )

        complete_step(conn, s["step_id"], output=output)
        prior_outputs[step_name] = output
        steps_run.append(step_name)

    # All steps walked successfully — finalize.
    complete_job(conn, job_id, result={"steps_run": steps_run})
    logger.info("worker=%s job=%s done (%d steps)", wid, job_id, len(steps_run))
    return TickResult(
        claimed=True, job_id=job_id, job_outcome="done", steps_run=steps_run,
    )


def run(
    *,
    job_type: str = "kol_create",
    max_jobs: int = 1,
    handlers: dict[str, StepHandler] | None = None,
) -> list[TickResult]:
    """Top-level entry point — open a DB connection and run up to *max_jobs* ticks.

    Returns one :class:`TickResult` per tick attempted. The systemd timer
    invokes this with ``--max-jobs 1``.
    """
    from sable_kol.db import open_db

    results: list[TickResult] = []
    with open_db() as conn:
        for _ in range(max_jobs):
            tr = run_one_tick(conn, job_type=job_type, handlers=handlers)
            results.append(tr)
            if not tr.claimed:
                break
    return results
