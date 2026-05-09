"""Tests for sable_kol.jobs (Phase C of the any-project KOL wizard build).

Covers:
    * Worker happy path (claim → walk steps → complete) with stubbed handlers
    * Resume idempotency: kill after survey_cohort_X, restart, no duplicate
      fetches, regenerate completes
    * Retry-then-fail when a step exhausts its retry budget
    * StepDeferred → release → re-claim flow (xAI 429 backoff path)
    * No-claim tick (returns claimed=False)
    * Cost-logging FK: cost_events row referencing jobs(job_id) succeeds
    * wizard_orgs.upsert_wizard_org idempotency
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from sable_platform.db.cost import log_cost
from sable_platform.db.jobs import add_step, create_job, get_resumable_steps
from sable_kol.jobs import StepDeferred, run_one_tick
from sable_kol.wizard_orgs import upsert_wizard_org


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_wizard_job(
    db_conn,
    *,
    org_id: str = "fashiondao",
    handle: str = "fashiondao",
    comparison_handles: tuple[str, ...] = ("metafactory", "rtfkt"),
    extra_steps: tuple[str, ...] = (),
) -> str:
    """Insert org + jobs(kol_create) + canonical job_steps and return job_id."""
    upsert_wizard_org(
        db_conn,
        org_id=org_id,
        display_name=org_id.title(),
        twitter_handle=handle,
        wizard_job_id="seed",
    )
    config = {
        "client_id": org_id,
        "handle": handle,
        "themes": ["fashion", "RWA"],
        "comparison_handles": list(comparison_handles),
        "network_axes": {
            "x": {"label": "fashion", "keywords": ["fashion", "luxury"]},
            "y": {"label": "crypto-native", "keywords": ["onchain", "defi"]},
        },
        "freshness_days": 180,
        "mode": "stealth",
    }
    job_id = create_job(db_conn, org_id, "kol_create", config=config)

    # Canonical step ordering per plan.
    order = 0
    for s in ("enrich", "suggest_comparable", "reuse_check"):
        add_step(db_conn, job_id, s, step_order=order)
        order += 1
    for h in comparison_handles:
        add_step(db_conn, job_id, f"survey_cohort_{h}", step_order=order)
        order += 1
    for s in ("write_yaml", "regenerate"):
        add_step(db_conn, job_id, s, step_order=order)
        order += 1
    for s in extra_steps:
        add_step(db_conn, job_id, s, step_order=order)
        order += 1
    return job_id


def _make_stub_handlers(
    *,
    survey_calls: dict[str, int] | None = None,
    fail_step: str | None = None,
    defer_step_name: str | None = None,
) -> dict:
    """Construct a handlers dict that records side-effects for assertions.

    *survey_calls* (if provided) is mutated in place: keys are handle names
    extracted from ``survey_cohort_<handle>`` step names, values are call counts.
    """
    survey_calls = survey_calls if survey_calls is not None else {}

    def enrich(ctx):
        if fail_step == "enrich":
            raise RuntimeError("synthetic enrich failure")
        if defer_step_name == "enrich":
            raise StepDeferred(retry_after_seconds=60, reason="synthetic 429")
        return {
            "handle": ctx.job_config["handle"],
            "recent_themes": ["fashion", "RWA"],
            "axis_candidates": [],
        }

    def suggest_comparable(ctx):
        if fail_step == "suggest_comparable":
            raise RuntimeError("synthetic suggest failure")
        return {"comparable_projects": [{"handle": "metafactory"}]}

    def reuse_check(ctx):
        return {"already_have": [], "must_fetch": list(ctx.job_config["comparison_handles"])}

    def survey_cohort(ctx):
        handle = ctx.step_name.removeprefix("survey_cohort_")
        survey_calls[handle] = survey_calls.get(handle, 0) + 1
        if fail_step == ctx.step_name:
            raise RuntimeError(f"synthetic {ctx.step_name} failure")
        return {"handle": handle, "profiles_kept": 42, "cursor_completed": True}

    def write_yaml(ctx):
        return {"yaml_path": f"/tmp/{ctx.job_config['client_id']}.yaml"}

    def regenerate(ctx):
        return {"regenerate": "ok", "client_id": ctx.job_config["client_id"]}

    return {
        "enrich": enrich,
        "suggest_comparable": suggest_comparable,
        "reuse_check": reuse_check,
        "survey_cohort": survey_cohort,
        "write_yaml": write_yaml,
        "regenerate": regenerate,
    }


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_no_jobs_returns_unclaimed(self, db_conn):
        result = run_one_tick(db_conn)
        assert result.claimed is False
        assert result.job_id is None
        assert result.job_outcome is None

    def test_claim_walk_complete(self, db_conn):
        survey_calls = {}
        handlers = _make_stub_handlers(survey_calls=survey_calls)
        job_id = _seed_wizard_job(db_conn)

        result = run_one_tick(db_conn, handlers=handlers)
        assert result.claimed is True
        assert result.job_id == job_id
        assert result.job_outcome == "done"
        assert "enrich" in result.steps_run
        assert "suggest_comparable" in result.steps_run
        assert "reuse_check" in result.steps_run
        assert "survey_cohort_metafactory" in result.steps_run
        assert "survey_cohort_rtfkt" in result.steps_run
        assert "write_yaml" in result.steps_run
        assert "regenerate" in result.steps_run

        # Survey was called once per handle.
        assert survey_calls == {"metafactory": 1, "rtfkt": 1}

        # Job is marked done.
        row = db_conn.execute(
            "SELECT status, completed_at, result_json FROM jobs WHERE job_id=?",
            (job_id,),
        ).fetchone()
        assert row["status"] == "done"
        assert row["completed_at"] is not None
        rj = json.loads(row["result_json"])
        assert rj["steps_run"] == result.steps_run

    def test_completed_steps_persist_output_json(self, db_conn):
        handlers = _make_stub_handlers()
        job_id = _seed_wizard_job(db_conn)
        run_one_tick(db_conn, handlers=handlers)

        steps = get_resumable_steps(db_conn, job_id)
        for s in steps:
            assert s["status"] == "completed", s["step_name"]
            assert s["output_json"] is not None
            assert json.loads(s["output_json"]) != {}


# ---------------------------------------------------------------------------
# Resume idempotency — Phase C "Required tests" #3
# ---------------------------------------------------------------------------


class TestResumeIdempotency:
    def test_resume_after_kill_no_duplicate_survey(self, db_conn):
        """Simulate worker crash mid-run after survey_cohort_metafactory.

        On the second tick, the worker MUST:
          * Not re-run completed steps (no duplicate SocialData fetch)
          * Run only the pending steps (survey_cohort_rtfkt, write_yaml, regenerate)
          * Mark the job done at the end
        """
        survey_calls = {}
        handlers = _make_stub_handlers(survey_calls=survey_calls)
        job_id = _seed_wizard_job(db_conn)

        # ---------- Tick 1: simulate the worker crashing after the first
        # survey_cohort step. We approximate this by hand-running the first
        # few steps and then NOT calling complete_job.
        from sable_platform.db.jobs import claim_next_job, complete_step, start_step

        claim = claim_next_job(db_conn, "kol_create", "worker-A")
        assert claim is not None and claim["job_id"] == job_id

        steps = get_resumable_steps(db_conn, job_id)
        for s in steps:
            if s["step_name"] in ("enrich", "suggest_comparable", "reuse_check", "survey_cohort_metafactory"):
                start_step(db_conn, s["step_id"])
                # Run handler manually to simulate work having been done.
                from sable_kol.jobs import StepContext
                ctx = StepContext(
                    conn=db_conn, job_id=job_id, org_id=claim["org_id"],
                    job_config=claim["config_json"], step_id=s["step_id"],
                    step_name=s["step_name"], step_input=json.loads(s["input_json"] or "{}"),
                    prior_outputs={},
                )
                output = handlers.get(
                    s["step_name"],
                    handlers["survey_cohort"] if s["step_name"].startswith("survey_cohort_") else None,
                )(ctx)
                complete_step(db_conn, s["step_id"], output=output)
        # Worker "crashed" — leave the job in 'running' status with worker_id=worker-A.

        # Force the job to be stale-reclaimable (otherwise the second tick won't
        # claim it within the default 10-min cutoff).
        db_conn.execute(
            "UPDATE jobs SET updated_at=datetime('now', '-15 minutes') WHERE job_id=?",
            (job_id,),
        )
        db_conn.commit()

        # ---------- Tick 2: a fresh worker stale-reclaims and finishes.
        survey_calls_t2 = dict(survey_calls)
        result = run_one_tick(db_conn, handlers=handlers)

        assert result.claimed is True
        assert result.job_id == job_id
        assert result.job_outcome == "done"

        # The first tick's survey calls (metafactory) were 1; second tick must
        # NOT have re-incremented metafactory but MUST have called rtfkt.
        assert survey_calls["metafactory"] == 1, "metafactory survey re-fetched on resume"
        assert survey_calls["rtfkt"] == 1, "rtfkt survey was not run on resume"

        # Steps actually run on tick 2 (skipped the completed ones).
        assert "enrich" not in result.steps_run
        assert "survey_cohort_metafactory" not in result.steps_run
        assert "survey_cohort_rtfkt" in result.steps_run
        assert "write_yaml" in result.steps_run
        assert "regenerate" in result.steps_run


# ---------------------------------------------------------------------------
# Retry budget exhaustion
# ---------------------------------------------------------------------------


class TestRetries:
    def test_step_below_retry_budget_releases_job(self, db_conn):
        """A failed step under its retry budget should release the job back
        to pending so the next tick can re-attempt."""
        handlers = _make_stub_handlers(fail_step="enrich")
        job_id = _seed_wizard_job(db_conn)

        result = run_one_tick(db_conn, handlers=handlers)
        assert result.claimed is True
        assert result.job_outcome == "released"

        row = db_conn.execute(
            "SELECT status FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        assert row["status"] == "pending"

        steps = get_resumable_steps(db_conn, job_id)
        enrich = next(s for s in steps if s["step_name"] == "enrich")
        assert enrich["status"] == "failed"
        assert enrich["retries"] == 1

    def test_step_exhausts_retry_budget_fails_job(self, db_conn):
        """Run the worker repeatedly until enrich's retries hit max_retries=3.

        After the third failure, the job should transition to ``failed``.
        """
        handlers = _make_stub_handlers(fail_step="enrich")
        job_id = _seed_wizard_job(db_conn)

        for _ in range(4):
            run_one_tick(db_conn, handlers=handlers)

        row = db_conn.execute(
            "SELECT status, error_message FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        assert row["status"] == "failed"
        assert "enrich" in row["error_message"]


# ---------------------------------------------------------------------------
# Deferred-retry path (StepDeferred)
# ---------------------------------------------------------------------------


class TestDeferred:
    def test_deferred_step_releases_job_and_sets_next_retry_at(self, db_conn):
        handlers = _make_stub_handlers(defer_step_name="enrich")
        job_id = _seed_wizard_job(db_conn)

        result = run_one_tick(db_conn, handlers=handlers)
        assert result.job_outcome == "released"

        steps = get_resumable_steps(db_conn, job_id)
        enrich = next(s for s in steps if s["step_name"] == "enrich")
        assert enrich["status"] == "pending"
        assert enrich["next_retry_at"] is not None
        # next_retry_at is in the future.
        nra = datetime.fromisoformat(enrich["next_retry_at"])
        if nra.tzinfo is None:
            nra = nra.replace(tzinfo=timezone.utc)
        assert nra > datetime.now(timezone.utc) - timedelta(seconds=1)

        # Job is back to pending.
        row = db_conn.execute("SELECT status FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        assert row["status"] == "pending"

    def test_deferred_step_skipped_when_next_retry_in_future(self, db_conn):
        """If a step's next_retry_at hasn't arrived yet, the worker should
        release the job and exit without running ANY handler."""
        handlers = _make_stub_handlers()
        job_id = _seed_wizard_job(db_conn)

        # Mark enrich as pending with a future next_retry_at.
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        db_conn.execute(
            "UPDATE job_steps SET next_retry_at=? "
            "WHERE job_id=? AND step_name='enrich'",
            (future, job_id),
        )
        db_conn.commit()

        result = run_one_tick(db_conn, handlers=handlers)
        assert result.job_outcome == "released"
        assert result.steps_run == []


# ---------------------------------------------------------------------------
# Cost-logging FK — Phase C "Required tests" #2
# ---------------------------------------------------------------------------


class TestCostLoggingFK:
    def test_cost_event_with_kol_create_job_id_succeeds(self, db_conn):
        upsert_wizard_org(
            db_conn,
            org_id="fashiondao",
            display_name="FashionDAO",
            twitter_handle="fashiondao",
            wizard_job_id="seed",
        )
        job_id = create_job(db_conn, "fashiondao", "kol_create", config={"x": 1})
        log_cost(
            db_conn,
            org_id="fashiondao",
            call_type="sablekol.socialdata_followers_page",
            cost_usd=0.002,
            model=None,
            job_id=job_id,
        )
        row = db_conn.execute(
            "SELECT cost_usd, job_id FROM cost_events WHERE job_id=?",
            (job_id,),
        ).fetchone()
        assert row is not None
        assert row["cost_usd"] == pytest.approx(0.002)
        assert row["job_id"] == job_id

    def test_cost_event_with_bogus_job_id_violates_fk(self, db_conn):
        """A cost_events row referencing a non-existent jobs.job_id must fail."""
        from sqlalchemy.exc import IntegrityError

        upsert_wizard_org(
            db_conn,
            org_id="fashiondao",
            display_name="FashionDAO",
            twitter_handle="fashiondao",
            wizard_job_id="seed",
        )
        with pytest.raises(IntegrityError):
            log_cost(
                db_conn,
                org_id="fashiondao",
                call_type="sablekol.socialdata_followers_page",
                cost_usd=0.002,
                model=None,
                job_id="bogus_job_id_does_not_exist",
            )


# ---------------------------------------------------------------------------
# wizard_orgs.upsert_wizard_org
# ---------------------------------------------------------------------------


class TestUpsertWizardOrg:
    def test_creates_inactive_org_with_prospect_config(self, db_conn):
        upsert_wizard_org(
            db_conn,
            org_id="fashiondao",
            display_name="FashionDAO",
            twitter_handle="fashiondao",
            wizard_job_id="job-uuid-123",
        )
        row = db_conn.execute(
            "SELECT status, twitter_handle, config_json, display_name "
            "FROM orgs WHERE org_id=?",
            ("fashiondao",),
        ).fetchone()
        assert row["status"] == "inactive"
        assert row["twitter_handle"] == "fashiondao"
        assert row["display_name"] == "FashionDAO"
        cfg = json.loads(row["config_json"])
        assert cfg["org_type"] == "prospect"
        assert cfg["created_via"] == "kol_wizard"
        assert cfg["wizard_job_id"] == "job-uuid-123"

    def test_idempotent_preserves_promoted_status(self, db_conn):
        """If operator promoted the org to status='active', a re-run of the
        wizard must NOT clobber that promotion."""
        # Initial creation — status='inactive'.
        upsert_wizard_org(
            db_conn,
            org_id="fashiondao",
            display_name="FashionDAO",
            twitter_handle="fashiondao",
            wizard_job_id="job-1",
        )
        # Operator promotes via the existing platform CLI path.
        db_conn.execute(
            "UPDATE orgs SET status='active' WHERE org_id=?",
            ("fashiondao",),
        )
        db_conn.commit()
        # Wizard re-run later — re-points wizard_job_id, leaves status alone.
        upsert_wizard_org(
            db_conn,
            org_id="fashiondao",
            display_name="FashionDAO",
            twitter_handle="fashiondao_v2",
            wizard_job_id="job-2",
        )
        row = db_conn.execute(
            "SELECT status, twitter_handle, config_json FROM orgs WHERE org_id=?",
            ("fashiondao",),
        ).fetchone()
        assert row["status"] == "active"
        assert row["twitter_handle"] == "fashiondao_v2"
        cfg = json.loads(row["config_json"])
        assert cfg["wizard_job_id"] == "job-2"


# ---------------------------------------------------------------------------
# Reuse module exports — sanity check the refactor
# ---------------------------------------------------------------------------


class TestReuseModuleExports:
    def test_cohorts_to_fetch_importable_from_reuse(self):
        from sable_kol.reuse import (
            COST_USD_PER_COHORT_FETCH,
            cohorts_to_fetch,
            estimate_fetch_cost_usd,
        )
        assert callable(cohorts_to_fetch)
        assert COST_USD_PER_COHORT_FETCH == 1.00
        assert estimate_fetch_cost_usd(["a", "b", "c"]) == 3.00

    def test_preflight_service_uses_shared_module(self):
        """Sanity check: preflight_service exports cohorts_to_fetch through
        the import name (verifies the refactor didn't drop the symbol)."""
        from sable_kol import preflight_service

        assert hasattr(preflight_service, "cohorts_to_fetch")
        assert hasattr(preflight_service, "estimate_fetch_cost_usd")
