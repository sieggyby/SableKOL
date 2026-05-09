# SableKOL TODO — Any-project wizard implementation

**Source of truth:** `docs/any_project_wizard_plan.md` (v3, Codex-audited round 2). Read this BEFORE doing any work. Every task below references a section.

**Last updated:** 2026-05-09, post Phase D landing — wizard complete end-to-end.

**Scope:** This TODO tracks implementation of the any-project KOL wizard ONLY. It is NOT the global "what's next" tracker — for that, see `~/Projects/Sable_Slopper/TODO.md` per existing convention.

---

## Status

- [x] **Pre-flight** — committed 2026-05-08 (`73b69e6`, `6ae2992`)
- [x] **Phase A — Foundation** — committed 2026-05-08, reviewed + greenlit
   - SablePlatform `b0a08b5` — migration 040 (1224 tests pass)
   - SableWeb `2322322` — allowlist + edge gate + button stub (165 tests pass)
- [x] **Phase B — Grok sidecar + preflight CLI** — committed 2026-05-09, greenlit
   - SableKOL `e32b428` — grok_api + preflight_service + schemas + CLI + Dockerfile + SIDECAR.md (36 new tests, 240 total pass)
   - SableWeb `74ac672` — docker-compose.yml sidecar block + DEPLOYMENT.md env docs
- [x] **Phase C — Claim helper + worker + reuse logic** — committed 2026-05-09
   - SablePlatform — `claim_next_job` + `complete_job`/`fail_job`/`release_job`/`defer_step` in `sable_platform/db/jobs.py` (13 new tests including 50-iteration race, 1237 total pass)
   - SableKOL — `sable_kol/jobs.py` worker + `sable_kol/reuse.py` (refactor) + `sable_kol/wizard_orgs.py` org auto-create + `sable-kol jobs run` CLI subcommand + `deploy/jobs/` systemd units (18 new tests, 258 total pass)
- [x] **Phase D — Wizard UI + status page** — committed 2026-05-09 (SableWeb `1fe5c09`)
   - 5 API routes (preflight, reuse-check, create, status, retry) with allowlist + audit + Zod boundary validation
   - 4-step wizard component (`KOLCreateWizard.tsx`) with AI-assisted chips, axis-pair picker, debounced reuse-preview, daily-quota check
   - Status page (`KOLJobStatus.tsx`) polling every 10s, per-step retry, auto-redirect on done
   - `WriteDriver.runMany()` for atomic org+job+steps create
   - 21 new tests (186 total pass, was 165)

---

## Pre-flight: pending state from prior session ✅ DONE

- [x] **Whitney Webb fix** — `sable_kol/filters.py` `_whitneywebb` denylist, committed `6ae2992`. Not yet deployed to prod (will ride next SableKOL sync to box).
- [x] **`deploy/migrate_kol_to_pg.py`** — committed as historical artifact (`73b69e6`). Do NOT re-run unless prod KOL tables need re-seeding.
- [ ] **SableWeb `src/components/ops/KOLNetwork.tsx`** — STILL uncommitted locally (zoom + axis-chrome already deployed via rsync 2026-05-07). Plus other unrelated changes in `src/app/api/intake`, `src/app/intake`, `src/app/proof/multisynq*`, `src/app/synq`, deleted `src/app/synq2`, `src/lib/db.ts`, `src/lib/db-write.ts`, `tests/api-intake.test.ts`. Phase A intentionally did NOT sweep these in.
- [x] **`docs/any_project_wizard_plan.md`** + `TODO.md` — committed (`73b69e6`).

---

## Phase A — Foundation ✅ DONE 2026-05-08

Plan section: "Phase A — Foundation".

### SableWeb (commit `2322322`)
- [x] `src/lib/kol-create-allowlist.ts` (edge-safe, pure data, 4 emails, lowercase-normalized check)
- [x] `src/lib/allowlist.ts` re-exports `canCreateKolProject` + `KOL_CREATE_EMAILS`
- [x] `src/middleware.ts` gates `/ops/kol-network/new` — decodes JWT inline, redirects to `/ops` if not allowlisted
- [x] `/ops/kol-network/page.tsx` renders "+ New project" button conditionally (empty-state path)
- [x] `/ops/kol-network/[clientId]/page.tsx` renders the same button in the header (the redirect-target path operators actually see)
- [x] `/ops/kol-network/new/page.tsx` stub — `getSession()` re-check + 403 stub for non-allowlisted; placeholder for allowlisted until Phase D
- [x] `tests/kol-create-allowlist.test.ts` — 6 tests (allowed, case-insensitive, broader-ops denied, null/empty)

### SablePlatform — Migration 040 (commit `b0a08b5`)
- [x] `sable_platform/db/migrations/040_kol_wizard_infra.sql` — `kol_create_audit` (email NULLABLE) + `jobs.worker_id` + `job_steps.next_retry_at` + 5 indexes. **Lesson re-encountered:** runner splits on raw `;` and `;` inside `--` comments creates phantom statements; rewrote comments to use `--` separators. Do NOT add explicit `BEGIN;`/`COMMIT;` (runner already wraps in `with conn:`).
- [x] `_MIGRATIONS` registered in `sable_platform/db/connection.py`
- [x] `sable_platform/db/schema.py` — `kol_create_audit` table + new columns + 5 indexes
- [x] Alembic revision `d8e0f1a2b040_kol_wizard_infra.py` — upgrade + downgrade
- [x] `migrate_pg.py` — `kol_create_audit` registered in `TABLE_LOAD_ORDER` + `SEQUENCE_TABLES`
- [x] `tests/db/test_migration_040.py` — SQLite parity + SA parity + Postgres path skipped without `SABLE_TEST_POSTGRES_URL`. Asserts nullable email + nullable worker_id + nullable next_retry_at.
- [x] `tests/db/test_kol_create_audit.py` — all 4 outcomes, NULL email accepted for `auth_failed`, FK enforced when set, NULL job_id accepted

### SableKOL
- [x] Whitney Webb fix committed (`6ae2992`)

### Phase A demo ✅
- Fresh-DB init verified: `sable-platform init` → `schema_version=40`, all three additions present.
- Button + 403 stub gate logic test-covered. Manual UI positive-path verification still requires `AUTH_ENABLED=true` + signing in as one of the 4 emails (dev session bypass returns `ops@sable.xyz` which is NOT on the wizard allowlist — correct negative-case behavior).

### Phase A deploy

NOT YET deployed to prod. Three repos to roll forward in order: SablePlatform → SableKOL → SableWeb. Each is a separate `git pull` + restart cycle on the relevant service. SablePlatform needs `alembic upgrade head` against the live Postgres DB before SableKOL or SableWeb pull.

---

## Phase B — Grok service (sidecar container) + preflight CLI ✅ DONE 2026-05-09

Plan section: "Grok API integration — explicit boundary".

### SableKOL (commit `e32b428`)
- [x] `sable_kol/grok_api.py` — pure xAI client. `enrich_handle(handle)`, `suggest_comparable_projects(handle, themes)`, `build_preflight_response(handle)`, `build_suggest_comparable_response(handle, themes)`. Pydantic-validated. Retry/backoff on 5xx + 429. GrokAuthError / GrokAPIError / GrokParseError taxonomy maps to sidecar 503/502.
- [x] `sable_kol/preflight_service.py` — FastAPI on `0.0.0.0:8001`. POST /preflight, /suggest-comparable, /reuse-check. Token-free /healthz for compose healthcheck. `secrets.compare_digest` token gate. Hard-fails 503 on missing `SABLE_SERVICE_TOKEN`. `cohorts_to_fetch` dual-driver (`?` positional + ISO-8601 string comparison).
- [x] `sable-kol preflight <handle>` CLI — `--enrich-only` and `--themes` flags. Prints JSON dump of what the wizard would pre-fill. No DB writes.
- [x] `sable_kol/preflight_schemas.py` — SignalMetadata, AxisPair, ComparableProject, EnrichedHandle, PreflightResponse, ReuseCheck request/response. `signal_metadata` carries source/model/fetched_at_utc/signal_type/caveat.
- [x] `Dockerfile.preflight` — Python 3.12 slim. Builds from parent of SableKOL+SablePlatform. Installs SP editable + `sable-kol[service]`. uvicorn entrypoint, healthcheck against /healthz.
- [x] `pyproject.toml` `[service]` extra — `fastapi>=0.110`, `uvicorn[standard]>=0.27`.
- [x] `deploy/SIDECAR.md` — build / first-deploy / token-rotation runbook.
- [x] `.env.example` — XAI_API_KEY + SABLE_SERVICE_TOKEN + SABLE_KOL_SERVICE_URL with hard-fail-on-missing semantics.
- [x] **NO CI build + push** — local `docker build` on prod box, per Sieggy's call. SIDECAR.md documents the build command.

### SableWeb (commit `74ac672`)
- [x] `docker-compose.yml` patched — `sable-kol-preflight` service block (no `ports:`, only compose-network), `depends_on: condition: service_healthy` on `web`, `SABLE_KOL_SERVICE_URL` + `SABLE_SERVICE_TOKEN` on `web`. xAI key only on the sidecar's env.
- [x] `docs/DEPLOYMENT.md` updated — env table adds `SABLE_SERVICE_TOKEN` (required) + `SABLE_KOL_SERVICE_URL` (optional). New "KOL wizard sidecar" section documents the env split + build command. `.env.example` is `.gitignore`d in SableWeb (line 34: `.env*`); DEPLOYMENT.md is the source of truth.

### Tests
- [x] tests/test_grok_api.py — 16 tests: missing key, 401/403, happy path, normalization, retry-success, retry-exhausted, 429 backoff, non-JSON content, unexpected response shape, schema violation, comparable parse failures, self-reference filter, build_preflight_response signal_metadata.
- [x] tests/test_preflight_service.py — 17 tests: token gate (parametrized over 3 endpoints × missing/wrong/unconfigured), preflight happy path with mocked Grok, xAI auth/parse failure mapping, /reuse-check splits/stale/partial-run/wrong-extract-type/empty-handles/normalization. Local `threaded_db_conn` fixture (StaticPool + check_same_thread=False) for TestClient thread-hop compatibility.
- [x] tests/test_preflight_cli.py — 3 tests: default bundled, --enrich-only, --themes override.
- [x] **NO live CI smoke against @solstitch** — operator-triggered via `sable-kol preflight solstitch`, NOT in CI (would bill xAI on every run).

### Phase B demo ✅
- All Phase B tests green: 36/36 in 0.41s. Full SableKOL suite 240 passed, 4 skipped (eval), no regressions.
- `sable-kol preflight --help` lists the new subcommand surface.
- `docker compose config` parses cleanly with both services present, depends_on health-gating wired.
- Could not `docker build` the image locally (daemon not running on dev laptop) — verified at deploy time on prod box per SIDECAR.md.

### Phase B deploy

NOT YET deployed to prod. Order: SablePlatform (Phase A migration 040) → SableKOL pull + local image build → SableWeb pull + `docker-compose up -d`. Both `XAI_API_KEY` and `SABLE_SERVICE_TOKEN` (32-byte hex from `openssl rand -hex 32`) must be in `/opt/sable/.env` BEFORE compose up.

---

## Phase C — Claim helper + worker + reuse logic ✅ DONE 2026-05-09

Plan section: "Worker model" + "Reuse detection".

### SablePlatform — `claim_next_job` (and lifecycle helpers) ✅
- [x] `sable_platform/db/jobs.py` — `claim_next_job(conn, job_type, worker_id, stale_after_minutes=10)` dual-driver:
   - Postgres: `SELECT ... FOR UPDATE SKIP LOCKED` inside `UPDATE` subquery
   - SQLite: `UPDATE ... WHERE job_id = (SELECT ... LIMIT 1) RETURNING` — single-statement atomicity, serialized by SQLite's database-level write lock
   - Both bump `jobs.updated_at`, stamp `jobs.worker_id`, return `{job_id, config_json, org_id}`
- [x] Companion lifecycle helpers in same module: `complete_job(job_id, result)`, `fail_job(job_id, error)`, `release_job(job_id)` (used when worker defers), `defer_step(step_id, retry_at_iso)` (sets `next_retry_at` for 429 backoff path).
- [x] `tests/db/test_claim_next_job.py` — 13 tests including:
   - Single claim → complete → second claim returns None
   - Two-racer race test (50 iterations, file-backed SQLite + WAL + busy_timeout, exactly one wins each iteration)
   - Stale reclaim (`updated_at` 11 min ago)
   - Wrong job_type returns None even with pending KOL jobs
   - `complete_job`/`fail_job`/`release_job`/`defer_step` round-trips
   - Postgres parity test gated on `SABLE_TEST_POSTGRES_URL` (skipped in normal runs)

### SableKOL — worker ✅
- [x] `sable_kol/jobs.py`:
   - `run_one_tick()` claims one job, walks `job_steps` in order, dispatches to step handlers, finalizes
   - Step handlers as a dispatch table (default real handlers + tests inject stubs)
   - Step machine: `enrich` (3 retries), `suggest_comparable` (3), `reuse_check` (0), `survey_cohort_<handle>` (2), `write_yaml` (0), `regenerate` (1)
   - `StepDeferred` exception → `defer_step` + `release_job` so 429 backoff defers cleanly
   - Honors `next_retry_at`: pending step with future `next_retry_at` releases the job instead of running
   - On retry-budget exhaustion → `fail_job`; below budget → `release_job` so next tick re-attempts
- [x] `sable-kol jobs run --job-type kol_create --max-jobs 1 [--json]` CLI subcommand
- [x] **`sable_kol/reuse.py`** — `cohorts_to_fetch` + `estimate_fetch_cost_usd` lifted out of `preflight_service.py` so the worker imports without dragging FastAPI in. Existing `tests/test_preflight_service.py::test_reuse_check_*` (8 tests) still pass post-refactor.
- [x] **`sable_kol/wizard_orgs.py`** — `upsert_wizard_org()` upserts `orgs` with `status='inactive'` + `config_json={"org_type":"prospect", "created_via":"kol_wizard", "wizard_job_id":<uuid>}`. Idempotent: re-runs preserve operator-set `status='active'` and only refresh `wizard_job_id` + `twitter_handle`.
- [x] `deploy/jobs/sable-kol-jobs.service` + `sable-kol-jobs.timer` + `README.md` — 60s tick, `RandomizedDelaySec=10s`, `OnBootSec=60s`, `--max-jobs 1`. Reads `XAI_API_KEY` + `ANTHROPIC_API_KEY` + `SABLE_DATABASE_URL` from `/etc/sable/sable-kol-jobs.env`.

### Tests ✅
- [x] `tests/test_jobs.py` — 14 worker tests covering happy path, resume idempotency (`TestResumeIdempotency.test_resume_after_kill_no_duplicate_survey` — kills worker post-`survey_cohort_metafactory`, asserts second tick re-runs only `survey_cohort_rtfkt` + `write_yaml` + `regenerate` and finishes), retry-then-release, retry-exhaustion → fail, deferred-step release, deferred-step skip-when-future, no-claim tick.
- [x] `TestCostLoggingFK.test_cost_event_with_kol_create_job_id_succeeds` + `test_cost_event_with_bogus_job_id_violates_fk` — FK enforced on both directions.
- [x] `TestUpsertWizardOrg` — creates inactive prospect, idempotent across promotion.
- [x] `TestReuseModuleExports` — sanity check `sable_kol.reuse` exports + that the refactor didn't drop the symbol from `preflight_service`.

### Phase C demo ✅
Operator manually creates `jobs` + `job_steps` rows via Python REPL; the worker picks up via `claim_next_job`, walks the step machine, completes the job. Race test proves two workers can't double-claim. Resume idempotency test proves a crash mid-run doesn't double-fetch SocialData.

### Phase C deploy

NOT YET deployed to prod. Order when deploy is greenlit (combines Phase A + B + C):
1. SablePlatform `alembic upgrade head` against the live Postgres DB
2. SableKOL `git pull` + `docker build -f SableKOL/Dockerfile.preflight -t sable-kol-preflight:latest .` from `/opt/sable` parent
3. SableKOL systemd timer install: `sudo cp deploy/jobs/sable-kol-jobs.{service,timer} /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now sable-kol-jobs.timer` — first create `/etc/sable/sable-kol-jobs.env` with `XAI_API_KEY` + `ANTHROPIC_API_KEY` + `SABLE_DATABASE_URL`
4. SableWeb `git pull` + `docker-compose up -d`. `XAI_API_KEY` and `SABLE_SERVICE_TOKEN` must be in `/opt/sable/.env` BEFORE compose up.

---

## Phase D — Wizard UI + status page ✅ DONE 2026-05-09

Plan section: "Wizard flow (4 steps)".

### SableWeb (commit `1fe5c09`)

**API routes — every one allowlist-gated + audited on every hit (4 outcomes: allowed/denied/quota_exceeded/auth_failed). Anonymous returns 401 + email=NULL audit row per Codex round-2 #5.**

- [x] `src/app/api/ops/kol-network/preflight/route.ts` — proxy to sidecar `/preflight`, validates response with Zod (mirrors `sable_kol/preflight_schemas.py`), maps sidecar 503/502 through.
- [x] `src/app/api/ops/kol-network/preflight/reuse-check/route.ts` — proxy to sidecar `/reuse-check`, debounced 300ms by the wizard UI.
- [x] `src/app/api/ops/kol-network/create/route.ts` — daily-quota gate (5/operator/day, counted from `kol_create_audit` outcome='allowed' rows in the last 24h), atomic create via new `WriteDriver.runMany()` (orgs upsert with status='inactive' + config_json prospect block + jobs row + 7 job_steps rows in a single tx).
- [x] `src/app/api/ops/kol-network/job/[id]/route.ts` — GET, submitter-or-admin rule (`session.role === 'admin'` OR `session.email === job.config.submitted_by_email`).
- [x] `src/app/api/ops/kol-network/job/[id]/retry/route.ts` — POST, same auth rule, resets failed step to `pending` + retries=0, flips job back to `pending` so worker re-claims.

**Wizard UI**
- [x] `src/components/ops/KOLCreateWizard.tsx` — 4-step client component:
   - Step 1 Identify: handle input + auto-derived slug + display_name (operator can override)
   - Step 2 Tags+Axes: editable theme chips, axis-pair picker (Grok candidates + manual fixed-library fallback), mode picker (stealth/public)
   - Step 3 Comparison projects: Grok suggestions with checkboxes + custom-handle add + live debounced reuse-preview ("Reusing N/M cohorts (180-day freshness), fetching K new ones, est ~$X")
   - Step 4 Confirm: review pane + cost ceiling + ETA, redirects to status page on submit
- [x] AI-assisted chips + freshness timestamps on every Grok-derived field per AGENTS signal taxonomy.
- [x] `src/app/ops/kol-network/new/page.tsx` — replaced Phase A 403 stub with the wizard mount (server-component still does `getSession()` + `canCreateKolProject` re-check before mounting).

**Status page**
- [x] `src/components/ops/KOLJobStatus.tsx` — polls every 10s, step-by-step status dots, per-step Retry button on failed steps, auto-redirect to `/ops/kol-network/<slug>` on done.
- [x] `src/app/ops/kol-network/job/[id]/page.tsx` — server-component gate + mounts the status component.

**Lib**
- [x] `src/lib/kol-create-audit.ts` — `recordAudit()` writes `kol_create_audit` rows via `getWriteDriver()`. `extractAuditIp()` prefers `x-real-ip` over `x-forwarded-for` (rightmost-only).
- [x] `src/lib/kol-create-gate.ts` — `withWizardGate()` is the shared 401/403/audit flow used by every route. `fetchSidecar()` reads `SABLE_KOL_SERVICE_URL` + `SABLE_SERVICE_TOKEN` from env so neither reaches the browser bundle.
- [x] `src/lib/kol-create-job.ts` — `buildStepNames()`, `createWizardJob()` (atomic via `runMany`), `checkDailyQuota()`, `getWizardJob()`, `getWizardSteps()`, `retryStep()`.
- [x] `src/lib/kol-create-schemas.ts` — Zod schemas matching `sable_kol/preflight_schemas.py` field-for-field.
- [x] `src/lib/db-write.ts` — added `WriteDriver.runMany()` for atomic multi-statement writes (Postgres: single client + BEGIN/COMMIT/ROLLBACK; SQLite: `db.transaction(fn)`).

### Tests
- [x] `tests/api-kol-network-auth.test.ts` — 20 tests covering anonymous→401+auth_failed, non-allowlisted→403+denied, allowed→200+allowed, quota→429+quota_exceeded, sidecar schema drift→502, submitter-or-admin (admin override + non-admin operator who isn't the submitter→403), invalid body→400, step-not-failed→409, atomic 9-statement create.
- [x] `tests/integration-kol-wizard.test.ts` — 1 end-to-end test: preflight → create (asserts atomic 9-statement runMany with correct config_json shape including `submitted_by_email`) → simulated worker writes YAML → `discoveredClientIds()` includes the new slug.

Full vitest: 186 passed, 21 new (was 165). Type-check + lint clean.

### Phase D demo
End-to-end. Operator logs in (allowlisted email), clicks "+ New project", types a handle, the wizard pre-fills via Grok, operator picks axes + cohorts, submits. Status page renders, polls. Worker (running on systemd timer) claims and walks the steps. On `done`, browser auto-redirects to the new project's network page (`/ops/kol-network/<slug>`).

### Phase D deploy

Combined with Phase A+B+C — see "Phase C deploy" above for the full rollout order. Phase D-specific reminders:
- SableWeb compose patch already includes the sidecar service (Phase B `74ac672`); after Phase D pull, just `docker-compose up -d` to pick up the new wizard pages + API routes.
- `SABLE_SERVICE_TOKEN` must be in `/opt/sable/.env` BEFORE compose up so the sidecar accepts the proxy's `X-Sable-Service-Token` header.
- Daily-quota counter resets naturally on a 24h sliding window — no cron needed.

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
