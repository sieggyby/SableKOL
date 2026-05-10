# SableKOL — Audit Log

Compact record of major shipped work. New work goes here only after it lands in `main` and is verified in prod (or explicitly demoted to historical artifact).

For active work, see `TODO.md`. For design rationale, see `docs/any_project_wizard_plan.md`.

---

## 2026-05-09 — Any-project KOL wizard, full end-to-end

The wizard takes a Twitter handle, runs Grok preflight (enrich + suggest comparable projects), proxies through SableWeb to a sidecar service, dispatches a job, walks a step machine on a 60s systemd worker, surveys cohorts via SocialData, generates the YAML, and renders the project network on `/ops/kol-network/<slug>`. Operator can monitor + retry per-step on the status page. End-to-end e2e test on prod completed 2026-05-09.

### Spec
- Plan: `docs/any_project_wizard_plan.md` (v3, Codex-audited round 2)

### Phase A — Foundation (2026-05-08)
- **SablePlatform** `b0a08b5` — migration 040: `kol_create_audit` (email NULLABLE), `jobs.worker_id`, `job_steps.next_retry_at`, 5 indexes. SQL + Alembic + parity test. Lesson: never put `;` in `--` comments inside `.sql` migrations (runner splits on `;` literally).
- **SableWeb** `2322322` — `kol-create-allowlist.ts` (edge-safe, 4 emails), `middleware.ts` gate on `/ops/kol-network/new`, conditional "+ New project" button on both empty-state and redirect-target pages, 6 allowlist tests.
- **SableKOL** `6ae2992` — Whitney Webb fix (filters denylist).

### Phase B — Grok sidecar + preflight CLI (2026-05-09)
- **SableKOL** `e32b428` — `grok_api.py` (xAI client, retry/backoff, error taxonomy), `preflight_service.py` (FastAPI on `:8001`, `secrets.compare_digest` token gate, `/preflight` + `/suggest-comparable` + `/reuse-check`), `preflight_schemas.py`, `Dockerfile.preflight`, `[service]` extra, SIDECAR.md runbook, `.env.example`. 36 new tests.
- **SableWeb** `74ac672` — `docker-compose.yml` sidecar block, `DEPLOYMENT.md` env split section.
- Architectural decision: sidecar container in compose network (no `host.docker.internal` cross-platform brittleness, xAI key isolated to sidecar env).

### Phase C — Claim helper + worker + reuse logic (2026-05-09)
- **SablePlatform** — `claim_next_job(conn, job_type, worker_id, stale_after_minutes=10)` in `sable_platform/db/jobs.py`, dual-driver (Postgres `FOR UPDATE SKIP LOCKED`, SQLite single-statement `UPDATE...RETURNING`), companion `complete_job` / `fail_job` / `release_job` / `defer_step`. 13 new tests including 50-iteration two-thread race + Postgres parity test gated on `SABLE_TEST_POSTGRES_URL`.
- **SableKOL** `405dc6a` — `sable_kol/jobs.py` worker (`run_one_tick`, step machine, `StepDeferred` for 429 backoff, per-step retry budgets), `sable_kol/reuse.py` (refactor of `cohorts_to_fetch` + `estimate_fetch_cost_usd` out of `preflight_service.py` so worker doesn't drag FastAPI), `sable_kol/wizard_orgs.py` (idempotent prospect-org upsert), `sable-kol jobs run` CLI, systemd timer at `deploy/jobs/sable-kol-jobs.{service,timer}`. 18 new tests.

### Phase D — Wizard UI + status page (2026-05-09)
- **SableWeb** `1fe5c09` — 5 API routes (preflight, reuse-check, create, status, retry) all behind `withWizardGate()` + `recordAudit()`, `KOLCreateWizard.tsx` 4-step component (handle → tags+axes → comparison projects → confirm) with AI-assisted chips + freshness timestamps + debounced reuse preview + 5/operator/24h quota check, `KOLJobStatus.tsx` poll-every-10s + per-step retry, `WriteDriver.runMany()` for atomic 9-statement create. Auth rule: submitter-or-admin. 21 new tests.
- **SableWeb** `41b72c1` — Ops nav: KOL Network link added between Actions and My Account, prefix-match active highlighting.

### Production deploy (2026-05-09)
- All three repos pulled to Hetzner VPS at `root@178.156.204.125`.
- Sidecar `docker build -f SableKOL/Dockerfile.preflight -t sable-kol-preflight:latest .` from `/opt/sable` parent. Compose up.
- `XAI_API_KEY` + `SABLE_SERVICE_TOKEN` (32-byte hex via `openssl rand -hex 32`) wired into `/opt/sable/.env`.
- systemd timer `sable-kol-jobs.timer` enabled, 60s tick + 10s jitter.
- Postgres `listen_addresses` reconfigured to bind docker bridges (172.17.0.1, 172.18.0.1) so the web container could reach Postgres.
- Web container's `/opt/sable-web/.env` patched: added `SABLE_DATABASE_URL` line + `host.docker.internal` substitution for `127.0.0.1`. Pre-existing config drift (web was reading a stale April-6 SQLite snapshot).

### Post-launch tactical fixes (same day)
- `bf437dd` — Bumped `GROK_MODEL` from `grok-2-latest` → `grok-4-latest`. xAI deprecated v2 mid-deploy; fixed in `grok_api.py` + tests + SIDECAR.md.
- `77f5237` — Bumped xAI client timeouts 30s → 90s for `enrich_handle` + `suggest_comparable_projects` + builder helpers. Added `field_validator` to coerce `None` → `""` for `bio` and `audience_archetype` (Grok returns `null` for unknown fields, breaking Pydantic validation).
- `9f20e42` — Replaced 11 occurrences of SQLite-only `datetime('now')` with portable `CURRENT_TIMESTAMP` across `sable_kol/{db.py,enrich.py,grok_import.py,cross_platform.py,socialdata_bulk.py}`. Found when bulk-fetch tried `mark_run_completed` against Postgres prod.
- `bd5794d` — Cost accounting rebase: SocialData charges per-RESULT (not per-page). Replaced `COST_USD_PER_PAGE = 0.002` with `COST_USD_PER_RESULT = 0.0002`; logged costs were ~3× actual. Memory `feedback_cost_estimate_framing.md` rewritten.
- **SableWeb** `ee5fb87` — Bumped grok-2-latest → grok-4-latest in test fixture strings.

### Verification — wizard live in prod
- 2026-05-09 e2e run: real handle submission completed full step machine (preflight → reuse_check → survey × N → write_yaml → regenerate → done). xAI 503 "model at capacity" hit on one step, retried successfully on attempt 3 (worker retry budget honored).
- Operator UI flow verified: allowlist → wizard → status page → auto-redirect to `/ops/kol-network/<slug>`.

### Out of scope (unchanged from plan)
- Generalized cross-client `cohort_pool` abstraction (per-survey reuse only).
- Per-vertical decay strategy (180-day window is one global knob).
- Cancel-running-job button (operator must wait or SSH).
- Cross-operator job visibility (siloed by submitter except admin override).
- Bulk job operations.
- Self-serve client login.
- Cross-client `cohort_pool` abstraction (Phase 2 generalization).
- Bulk tag/cohort editing.
- Operator-typed custom axes (fixed library only).
- Slack/email completion notifications.
- "Delete project" web flow.

---

## 2026-05-06 — SolStitch KOL outreach pipeline

`4385ae9` — Initial commit. SablePlatform migration 037 + SableKOL `bulk_followers` / `follow_graph` / `outreach_plan` modules + CLI + 47 tests. SolStitch top-100 outreach plan generated and downloadable from `/ops/kol-network/<slug>`.

---

## 2026-05-07 — KOL Network viewer + relationship tagging

**SableWeb** `645556c` — `/ops/kol-network/[clientId]` route with d3-force layered network viewer, axis-scored 2D layout, outreach-plan downloads, manual relationship-tagging UI. Deployed via rsync to prod 2026-05-07; component code itself was not committed at the time and was intentionally held out of Phase A's git sweep.

---

## 2026-05-09 — KO-1: preflight operator-priming flags

**SableKOL** `6551693` — `--context` / `--exclude-handles` / `--allow-research` flags on `sable-kol preflight` for non-fashion/web3 clients. `enrich_handle` / `suggest_comparable_projects` / `build_preflight_response` accept the new kwargs and thread into `_build_enrich_prompt` / `_build_comparable_prompt`. `FIXED_AXIS_LIBRARY` gained 5 research/AI/DeSci axes (`research-academic`, `ai-ml`, `desci-science`, `algorithmic-quant`, `e-acc-frontier`). 10 new tests across `test_grok_api.py` (CLI passthrough + prompt builder behavior) and `test_preflight_cli.py` (default + each flag + combined themes-override path). Total suite: 268 passed (was 258).

Sidecar passthrough deferred to KO-1.b — `preflight_service.py` does not yet plumb the kwargs through, so wizard UI can't reach them. Bundled into KO-3 Phase 2 in the open TODO.

## 2026-05-09 — KO-2: KOLNetwork zoom + pan + hide-unscored

**SableWeb** `af8dbbe` — Zoom + pan viewport on the network viewer (mouse-wheel + pinch + click-drag, ZOOM_MIN=0.2/ZOOM_MAX=8). World coords from d3-force; screen coords = world × zoom + pan. Axes/edges/nodes/labels share one transform; axis-label chrome restored to screen space inside the same render pass so labels remain readable at any zoom. Default-on `hideUnscored` toggle hides the ~91% of nodes that pile at axis-zero when only one axis carries signal. Pure client-side; no API or data shape changes. Already deployed via rsync 2026-05-07; this commit was the working-tree catch-up.

## 2026-05-09 — Docs split: AUDIT_LOG + slim TODO

**SableKOL** `1957e32` — Created this audit log; rewrote `TODO.md` from 258 → ~80 lines focused on open work. `targets/` (operator-curated outreach artifacts) added to `.gitignore`.
