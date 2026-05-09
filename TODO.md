# SableKOL TODO — Any-project wizard implementation

**Source of truth:** `docs/any_project_wizard_plan.md` (v3, Codex-audited round 2). Read this BEFORE doing any work. Every task below references a section.

**Last updated:** 2026-05-08, Sieggy + Claude session signing off pre-implementation.

**Scope:** This TODO tracks implementation of the any-project KOL wizard ONLY. It is NOT the global "what's next" tracker — for that, see `~/Projects/Sable_Slopper/TODO.md` per existing convention.

---

## Pre-flight: pending state from prior session

Before opening the plan, settle these so a fresh Claude isn't blindsided:

- [ ] **Whitney Webb fix** — `sable_kol/filters.py` has `_whitneywebb` added to `CELEBRITY_DENYLIST` locally, NOT committed, NOT deployed. Decide: commit + deploy now, or batch with Phase A foundation work. (See "Whitney Webb fix" section in the plan.)
- [ ] **`deploy/migrate_kol_to_pg.py`** — uncommitted. Was used once 2026-05-07 to seed prod Postgres from the operator's local SQLite. Do NOT re-run unless prod KOL tables need re-seeding. Commit as historical artifact.
- [ ] **SableWeb `src/components/ops/KOLNetwork.tsx`** — zoom + axis-chrome changes are DEPLOYED to prod (via rsync 2026-05-07) but uncommitted in the local SableWeb repo. Other unrelated SableWeb changes are also uncommitted; don't sweep them in unless intentional.
- [ ] **`docs/any_project_wizard_plan.md`** itself is uncommitted in SableKOL. Commit as the canonical plan reference before starting work.

---

## Phase A — Foundation (~1.25 days)

Plan section: "Phase A — Foundation".

### SableWeb
- [ ] Create `src/lib/kol-create-allowlist.ts` (edge-safe, pure data). 4 emails: `george@arkn.io`, `ben@arkn.io`, `arfcahit1910@gmail.com`, `siegby@gmail.com`. Lowercase-normalized check.
- [ ] `src/lib/allowlist.ts` re-exports `canCreateKolProject` for non-edge consumers.
- [ ] `src/middleware.ts` gates `/ops/kol-network/new` — redirect to `/ops` if `!canCreateKolProject(session.email)`.
- [ ] `/ops/kol-network/page.tsx` renders the "+ New project" button conditionally.
- [ ] `/ops/kol-network/new/page.tsx` stub — re-checks via `getSession()`, returns 403 if false. Wizard UI lands in Phase D.

### SablePlatform — Migration 040 (full AGENTS-compliant spec)
Plan section: "Migration 040 — full SablePlatform AGENTS-compliant spec".

- [ ] Write `sable_platform/db/migrations/040_kol_wizard_infra.sql`:
   - `CREATE TABLE kol_create_audit` with `email TEXT NULL`
   - `ALTER TABLE jobs ADD COLUMN worker_id TEXT`
   - `ALTER TABLE job_steps ADD COLUMN next_retry_at TEXT`
   - 4 indexes (audit email, audit at, audit outcome, jobs worker, job_steps next_retry)
   - `UPDATE schema_version SET version = 40 WHERE version < 40`
- [ ] Add `("040_kol_wizard_infra.sql", 40)` to `_MIGRATIONS` in `sable_platform/db/connection.py`.
- [ ] Add `kol_create_audit` table + new columns to `sable_platform/db/schema.py`.
- [ ] Write Alembic revision at `sable_platform/alembic/versions/<hash>_kol_wizard_infra.py`. `upgrade()` AND `downgrade()` both required.
- [ ] Write `tests/db/test_migration_040.py`:
   - SQLite + Postgres parity (run all migrations, inspect schema)
   - Assert `kol_create_audit.email` is nullable on BOTH drivers
   - Assert `jobs.worker_id` and `job_steps.next_retry_at` are nullable on BOTH drivers
- [ ] Write `tests/db/test_kol_create_audit.py` insert-fixture tests:
   - `email IS NULL` accepted for `outcome='auth_failed'`
   - FK to `jobs(job_id)` enforced when `job_id` is set
   - All 4 outcome values (`allowed`/`denied`/`quota_exceeded`/`auth_failed`) succeed

### SableKOL
- [ ] Whitney Webb fix in `sable_kol/filters.py` already staged — confirm + commit.

### Phase A demo
Button visible to 4 ops, click → 403 stub. Migration applied + tested both drivers; nullable columns assertable.

---

## Phase B — Grok service (sidecar container) + preflight CLI (~1 day)

Plan section: "Grok API integration — explicit boundary".

### SableKOL
- [ ] Create `sable_kol/grok_api.py` — pure xAI client. Two functions: `enrich_handle(handle)`, `suggest_comparable_projects(handle, themes)`. Both return Pydantic-validated dicts.
- [ ] Create `sable_kol/preflight_service.py` — FastAPI app on `0.0.0.0:8001` (inside-container bind). Three endpoints: `POST /preflight`, `POST /suggest-comparable`, `POST /reuse-check`. All gated by `X-Sable-Service-Token` header.
- [ ] Add `sable-kol preflight <handle>` CLI subcommand — calls `enrich_handle` directly, prints what wizard would pre-fill. No DB writes, no service required.
- [ ] Define Pydantic response schemas with `signal_metadata` block (source, model, fetched_at_utc, signal_type, caveat).
- [ ] Create `Dockerfile.preflight` for the sidecar image. Minimal Python 3.12 slim; pip-install `sable_kol[paid-enrich]`; entry point uvicorn.
- [ ] CI build + push pipeline (or manual `docker build -t sable-kol-preflight:latest .` for v1).

### SableWeb
- [ ] Patch `docker-compose.yml` — add `sable-kol-preflight` service block (no published ports), `depends_on: [sable-kol-preflight]` on `web`, set `SABLE_KOL_SERVICE_URL: http://sable-kol-preflight:8001` and `SABLE_SERVICE_TOKEN: ${SABLE_SERVICE_TOKEN}` on `web`.
- [ ] Document `XAI_API_KEY` and `SABLE_SERVICE_TOKEN` env vars in `docs/DEPLOYMENT.md` (or wherever the existing env table lives).

### Tests
- [ ] Mock xAI response, validate Pydantic parsing.
- [ ] One real-world smoke against `@solstitch` — assert reasonable themes + comparable handles.
- [ ] Service-token rejection test: missing/wrong header → 403.

### Phase B demo
Sidecar running alongside SableWeb in compose; SableWeb route handler reaches it via `SABLE_KOL_SERVICE_URL`; xAI key never appears in SableWeb's environment. Operator runs `sable-kol preflight @somehandle` from terminal and sees what Grok would return.

---

## Phase C — Claim helper + worker + reuse logic (~1.5 days)

Plan section: "Worker model" + "Reuse detection".

### SablePlatform — `claim_next_job` helper (the helper that does NOT exist yet)
- [ ] Add `claim_next_job(conn, job_type, worker_id, stale_after_minutes=10)` to `sable_platform/db/jobs.py`. Dual-driver:
   - Postgres: `SELECT ... FOR UPDATE SKIP LOCKED` + `UPDATE` in one tx
   - SQLite: `BEGIN IMMEDIATE` + `UPDATE` with `RETURNING` (SQLite >= 3.35)
   - Both bump `jobs.updated_at` and set `jobs.worker_id`
   - Stale-reclaim path: re-claim `status='running' AND updated_at < (now - stale_after_minutes)`
- [ ] Write `tests/db/test_claim_next_job.py`:
   - Single claim, single complete, second claim returns None
   - Two simultaneous claimers race test (run 100 iterations, exactly one wins each iteration)
   - Stale reclaim: claim, set `updated_at` to 11 minutes ago, second claim succeeds
   - Wrong job_type: claim with `job_type='other'` returns None even when KOL jobs are pending
   - Both drivers (SQLite + Postgres parity)

### SableKOL — worker
- [ ] Create `sable_kol/jobs.py`:
   - Calls `claim_next_job(job_type='kol_create', worker_id=<uuid>)`
   - Walks `job_steps` in order, persists `output_json` per step
   - Honors `next_retry_at` (skip steps where `next_retry_at > now`)
   - Step machine per plan: `enrich`, `suggest_comparable`, `reuse_check`, `survey_cohort_<handle>` × N, `write_yaml`, `regenerate`
- [ ] Add `sable-kol jobs run` CLI subcommand (single-tick mode, called by systemd timer).
- [ ] Implement `cohorts_to_fetch(db, comparison_handles, freshness_days=180)` — dual-driver, `IN (...)` + ISO-string comparison.
- [ ] Implement org auto-create helper:
   - Upsert `orgs(org_id, display_name, twitter_handle, status='inactive', config_json={"org_type":"prospect", "created_via":"kol_wizard", "wizard_job_id":<uuid>})`
   - DO NOT use `org_type` or `is_active` columns (they don't exist on `orgs`)
- [ ] Create `deploy/jobs/sable-kol-jobs.service` + `.timer` (60s tick, RandomizedDelaySec=10s).

### Tests (per plan "Required tests" list)
- [ ] **Worker resume idempotency**: kill worker after `survey_cohort_X`, restart, assert no duplicate SocialData fetches; regenerate completes.
- [ ] **Cost-logging FK**: insert org, jobs row with `job_type='kol_create'`, cost_events row referencing the job → succeeds. FK violation when `job_id` is bogus.

### Phase C demo
Operator manually creates `jobs` + `job_steps` rows via Python REPL; worker picks them up via `claim_next_job`, runs end-to-end, generates YAML at `/opt/sable/clients/<slug>.yaml`, regenerate produces graph at `/opt/sable/outreach/<slug>/`. Run two workers in parallel — exactly one claims any given job.

---

## Phase D — Wizard UI + status page (~1 day)

Plan section: "Wizard flow (4 steps)".

### SableWeb — wizard UI
- [ ] `/ops/kol-network/new/page.tsx` — 4-step wizard component (Identify → Tags+axes → Comparison projects → Confirm).
- [ ] AI-assisted chips + freshness timestamps on every Grok-derived field (per AGENTS).
- [ ] Reuse-preview live debounce (300ms) on Step 3.
- [ ] Daily-quota check (5/day per operator) before submit.

### SableWeb — API routes (every route gated + audited)
- [ ] `POST /api/ops/kol-network/preflight/route.ts` — proxy to sidecar, allowlist gate, audit row on every hit.
- [ ] `POST /api/ops/kol-network/preflight/reuse-check/route.ts` — proxy to sidecar `/reuse-check`.
- [ ] `POST /api/ops/kol-network/create/route.ts` — validates, upserts org, inserts `jobs` + `job_steps` in one tx, returns `job_id`.
- [ ] `GET /api/ops/kol-network/job/[id]/route.ts` — submitter-or-admin rule.
- [ ] `POST /api/ops/kol-network/job/[id]/retry/route.ts` — resets failed step's status to `pending`, decrements retry count if at cap.

### SableWeb — status page
- [ ] `/ops/kol-network/job/[id]/page.tsx` — polls every 10s, shows step-by-step progress from `job_steps`.
- [ ] On `failed`: "Retry failed step" button.
- [ ] On `done`: redirect to `/ops/kol-network/<slug>`.

### Tests (per plan "Required tests" list)
- [ ] **SableWeb auth** at `tests/api-kol-network-auth.test.ts`:
   - Non-allowlisted email gets 403 on every route
   - Submitter-or-admin rule for status/retry
   - Audit row inserted on every hit (allowed AND denied)
   - Anonymous (no session) → 401, audit row with `email IS NULL`
- [ ] **End-to-end visibility** at `tests/integration-kol-wizard.test.ts`:
   - Mock sidecar, submit wizard, assert `jobs` row created
   - Mock worker output (write YAML + network JSON to expected paths)
   - Assert `discoveredClientIds()` returns the new slug
   - Assert `/ops/kol-network/<slug>` renders with non-empty data

### Phase D demo
End-to-end. Operator logs in, clicks "+ New project", types a handle, walks the wizard, watches status page, lands on the new project's network page when the worker finishes.

---

## Cross-repo coordination

Three repos touch this:
- **SablePlatform**: migration 040 + `claim_next_job` helper.
- **SableKOL**: Grok service + worker + sidecar Dockerfile + filters.py fix.
- **SableWeb**: allowlist module + middleware + wizard UI + API routes + compose patch.

**Order of merge** (to avoid cross-repo breakage):
1. SablePlatform migration 040 lands first; alembic upgrade head on prod.
2. SableKOL preflight service ships next (sidecar image built + pushed).
3. SableWeb compose patch + UI ships last (depends on both above).

**On prod**: each landing is a separate `git pull` + restart cycle on the relevant service. No cross-repo lockstep deploys required.

---

## Operational secrets (set BEFORE any production deploy)

These must be in `/opt/sable/.env` on the prod box:
- `XAI_API_KEY` — for Grok. Read from xAI dashboard.
- `SABLE_SERVICE_TOKEN` — random 32-byte hex. Generate: `openssl rand -hex 32`. Same value in BOTH the sidecar's env AND SableWeb's env. Rotate every 90 days (manual, document in `deploy/SECRETS.md`).
- `SABLE_KOL_SERVICE_URL` — already documented above; defaults `http://sable-kol-preflight:8001`.

---

## When in doubt

- Re-read `docs/any_project_wizard_plan.md` — it is the source of truth.
- The "Codex audit response" tables at the bottom of the plan map every Codex finding to the v3 fix.
- The plan's "Required tests" section is the validation contract for each phase.
- For graph-reuse architectural framing, see memory file `project_sablekol_graph_reuse.md`.

---

## Definitely NOT in scope (do not get pulled in)

- Self-serve client login
- A web "rerun project" button (still SSH + cron)
- Cross-client `cohort_pool` abstraction (Phase 2 generalization)
- Bulk tag/cohort editing
- Operator-typed custom axes (start with the fixed 10-label library)
- Slack/email completion notifications (in-app status page only for v1)
- "Delete project" web flow
