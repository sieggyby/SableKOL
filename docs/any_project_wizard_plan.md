# Any-project KOL wizard — build plan (v3, Codex-audited round 2)

**Status:** design, not yet built. v3 fixes the 5 blockers Codex flagged on v2 (Docker network boundary, missing claim helper, orgs schema mismatch, missing `next_retry_at`, NOT NULL email in audit).
**Author:** Sieggy + Claude collaboration. v1 2026-05-08, v2 2026-05-08 post-Codex round 1, v3 2026-05-08 post-Codex round 2.
**Companion docs:** `sableweb_kol_build_plan.md`, `sableweb_kol_network_plan.md`, `sableweb_kol_hetzner_deploy.md`, `sablekol_generalization_plan.md`.

---

## Goal

Let four named operators (Sparta, Ben, Sieggy, Arf) submit a Twitter handle through the SableWeb ops surface and get back a SolStitch-grade KOL outreach plan + 2D semantic-axis network — without SSH, without hand-authoring YAML, and without surveying the entire follower-graph from scratch every time.

## Scope

### In scope (this build)
- A new ops-only "+ New project" wizard at `/ops/kol-network/new`
- 4 named-email gate (`george@arkn.io`, `ben@arkn.io`, `arfcahit1910@gmail.com`, `siegby@gmail.com`)
- Grok API integration for live X handle lookup + comparable-project suggestion (replacing manual paste workflow `grok_import.py` already supports). Boundary: SableKOL HTTP service, NOT SableWeb-imported Python.
- **Reuse** the existing `jobs` + `job_steps` infrastructure as the queue. Worker owns the per-step state machine; nothing new in the queue domain.
- Cohort-reuse detection: skip SocialData fetches for handles already in `kol_extract_runs` within a freshness window
- Per-job cost preview + per-operator daily quota
- Whitney Webb fix already staged in `filters.py` (CELEBRITY_DENYLIST), ships with whatever rebuild lands first

### Out of scope (deferred)
- Self-serve client login (operator-only)
- A rerun/refresh button on existing projects (still SSH + cron until Phase 2)
- Cross-client `cohort_pool` abstraction (the architecturally correct generalization — see `project_sablekol_graph_reuse.md` memory)
- Bulk tag/cohort editing UI for existing projects
- Axis-library editor (start with a fixed library — see Open Questions)

---

## Architecture (revised — explicit Grok boundary)

```
┌─────────────────────────────────────────────────────────┐
│ Operator browser                                        │
│   /ops/kol-network → "+ New project" → /new (wizard)    │
└─────────────┬───────────────────────────────────────────┘
              │ all routes gated by KOL_CREATE_ALLOWLIST + audit
              ▼
┌─────────────────────────────────────────────────────────┐
│ SableWeb (Next.js, edge + Node)                         │
│   • middleware.ts: edge-safe allowlist gate             │
│   • /api/ops/kol-network/preflight (sync proxy)         │
│   • /api/ops/kol-network/create   (insert into jobs)    │
│   • /api/ops/kol-network/job/[id] (status, retry)       │
└──────┬──────────────────────────┬────────────────────────┘
       │ server-side fetch         │ DB write
       │ + service token           │ (jobs, job_steps,
       │                           │  kol_create_audit)
       ▼                           ▼
┌──────────────────────┐   ┌──────────────────────────────┐
│ sable-kol-preflight  │   │ Postgres / SQLite (dual)     │
│ SIDECAR CONTAINER    │   │   jobs + job_steps           │
│ in same compose net  │   │   cost_events (FK to jobs)   │
│   FastAPI on 0.0.0.0 │   │   kol_create_audit (new)     │
│   bound to compose   │   │   kol_extract_runs           │
│   network only,      │   │   kol_candidates             │
│   NO published port  │   └──────────────────────────────┘
│   POST /preflight    │              ▲
│   POST /suggest-     │              │
│        comparable    │              │
│   POST /reuse-check  │              │ writes via existing
│   xAI key here ONLY  │              │ jobs/job_steps state
└──────────────────────┘              │
   ▲                                  │
   │ http://sable-kol-preflight:8001  │
                                       │ writes via existing
                                       │ job_steps state machine
                                       │
┌──────────────────────────────────────┴───────────────────┐
│ Worker — sable-kol jobs run (systemd timer)              │
│   1. Claim a queued job_type='kol_create' job            │
│   2. For each pending step in job_steps:                 │
│      - enrich (Grok via local SableKOL service)          │
│      - suggest_comparable (Grok)  [skip if op-supplied]  │
│      - reuse_check (DB query, no spend)                  │
│      - survey_cohort_<handle> (SocialData) × N           │
│      - write_yaml → /opt/sable/clients/<slug>.yaml       │
│      - regenerate (calls existing run_regenerate())      │
│   3. Each step writes step_steps.output_json on success  │
│      so resumes are idempotent                           │
└──────────────────────────────────────────────────────────┘
```

---

## Job model — REUSE jobs + job_steps (Codex Critical #1)

**Decision: do NOT create a parallel `kol_jobs` table.** The existing `jobs` + `job_steps` schema (`migrations/001_initial.sql:115`, `004_jobs_extend.sql`) already has every field the wizard needs:

| Field needed | Existing column | Notes |
|---|---|---|
| Job ID | `jobs.job_id TEXT PRIMARY KEY` | Worker generates a UUID |
| Org attribution | `jobs.org_id REFERENCES orgs(org_id)` | NOT NULL — see "Org auto-create" below |
| Job kind discriminator | `jobs.job_type` | New value: `kol_create` |
| Wizard config | `jobs.config_json TEXT` | Stores the YAML the wizard built + submitter email + cost estimate + comparison handles |
| Status | `jobs.status` | `pending` → `running` → `done`/`failed` |
| Per-step retry | `job_steps.retries`, `job_steps.error` | Already there |
| Step checkpoint | `job_steps.output_json` | Idempotent resume relies on this |
| Cost FK target | `cost_events.job_id REFERENCES jobs(job_id)` | The original FK problem disappears |

**Implication:** the only new table this plan introduces is `kol_create_audit` for the auth log — see Migration 040 below.

**`jobs.config_json` shape for `job_type='kol_create'`:**
```json
{
  "submitted_by_email": "siegby@gmail.com",
  "submitted_at_utc": "2026-05-08T15:00:00Z",
  "wizard_version": "1",
  "client_id": "fashiondao",
  "handle": "fashiondao",
  "handle_twitter_id": null,
  "mode": "stealth",
  "themes": ["fashion", "RWA", "streetwear"],
  "network_axes": {"x": {"label": "fashion"}, "y": {"label": "crypto-native"}},
  "comparison_handles": ["solstitch", "metafactory", "rtfkt"],
  "cost_estimate_usd": 12.40,
  "cost_ceiling_usd": 50.00,
  "freshness_days": 180
}
```

**Org auto-create (Codex Critical #3, round 2):** the `orgs` table has columns `org_id, display_name, discord_server_id, twitter_handle, config_json, status, created_at, updated_at` — there is no `org_type` and no `is_active`. v3 fix: wizard submission upserts an `orgs` row with:
- `org_id = client_id` (wizard slug)
- `display_name` = wizard's display name
- `twitter_handle` = the handle being analyzed
- `status = 'inactive'` (existing convention; operator promotes via CLI later)
- `config_json = {"org_type": "prospect", "created_via": "kol_wizard", "wizard_job_id": "<job_uuid>"}`

This satisfies `jobs.org_id REFERENCES orgs(org_id)` without inventing columns. Promotion to `status='active'` happens via the existing `sable-platform org config set ...` CLI; this build does NOT add a web "promote org" button.

---

## Output paths (Codex Critical #2)

**Always go through `client_config` resolvers; NEVER hard-code `~/.sable`.**

| What | Resolver | Prod | Dev |
|---|---|---|---|
| Client YAML write | `client_config.PROD_CLIENT_DIR` (already exists at `client_config.py:?`) | `/opt/sable/clients/<slug>.yaml` | `~/.sable/clients/<slug>.yaml` |
| Outreach artifacts | `client_config.outreach_output_dir(client_id)` | `/opt/sable/outreach/<slug>/` | `~/Downloads/` (current dev convention) |

**SableWeb container mounts** (already configured per `sableweb_kol_hetzner_deploy.md`):
- `/opt/sable/clients` → `/sable/clients:ro`
- `/opt/sable/outreach` → `/sable/outreach:ro`

**Visibility check (smoke test):** wizard finishes, `discoveredClientIds()` (in `SableWeb/src/lib/client-config.ts`) MUST list the new slug, and `/ops/kol-network/<slug>` MUST render the new graph. This is one of the required tests below.

**Worker writes happen on the host, not in the container.** Worker runs as the `sable` user via systemd; it has direct write access to `/opt/sable/clients` and `/opt/sable/outreach`. SableWeb container reads via the bind-mount.

---

## Wizard flow (4 steps) — interpretive-signal labeling per AGENTS

### Step 1 — Identify
Inputs: `@handle`, `slug` (auto-derived, editable, regex-validated), `display name` (auto-derived, editable).

On Next:
- SableWeb POSTs to `/api/ops/kol-network/preflight` (sync, server-side)
- That route calls SableKOL service `POST localhost:<port>/preflight` with handle + service token
- Service runs `enrich_handle()` + `suggest_comparable_projects()` via xAI API
- Response includes the suggestions PLUS a `signal_metadata` block per AGENTS:
  ```json
  {
    "themes": ["fashion", "RWA", "streetwear"],
    "axis_candidates": [
      {"x": "fashion", "y": "crypto-native"},
      {"x": "luxury", "y": "on-chain"}
    ],
    "comparable_projects": [...],
    "signal_metadata": {
      "source": "grok_xai_live",
      "model": "grok-2-latest",
      "fetched_at_utc": "2026-05-08T15:00:00Z",
      "signal_type": "interpretive",
      "caveat": "AI-suggested; operator should confirm against on-platform context"
    }
  }
  ```
- Wizard pre-fills steps 2 + 3, prominently displaying the AI-assisted chip + freshness timestamp on every Grok-derived field

### Step 2 — Tags + axes (operator review)
- Themes chips: editable (add/remove), each Grok-suggested chip carries the AI-assisted badge
- **Axis pair picker**: 2-3 candidate `(x, y)` pairs Grok proposed; operator picks one OR opens a constrained dropdown from the fixed axis library (see Open Questions). Fixed library (initial): `{fashion, luxury, streetwear, technical-credibility, crypto-native, degen-coded, cultural-relevance, consumer-mainstream, on-chain, defi-native}`.
- Mode: stealth | public (default stealth)

### Step 3 — Comparison projects (the lynchpin)
- Display Grok's 8-10 proposals; each row shows the AI-assisted chip + a one-line context
- **Pre-emptive filtering**: hide org/celebrity matches by default (filters from `filters.py`); show with warning chip if operator opts in
- Operator checks 3-7 they want surveyed; can also type in handles Grok missed
- **Reuse preview** (live, debounced 300ms):
  - SableWeb POSTs the candidate list to `/api/ops/kol-network/preflight/reuse-check`
  - Response: `{"already_have": [...], "must_fetch": [...], "estimated_cost_usd": 8.50}`
  - UI shows: *"Reusing 3/5 cohorts (within 180-day freshness), fetching 2 new ones, est ~$8.50"*

### Step 4 — Confirm
- Final cost estimate (Grok + SocialData + Haiku rationales)
- ETA (1 cohort ≈ 2-4 minutes serial)
- "Submit" → API inserts `orgs` (if needed) + `jobs` + `job_steps` rows in a single transaction → returns `job_id` + status URL

### Status page
- Polls `/api/ops/kol-network/job/[id]` every 10s
- Shows step-by-step progress from `job_steps` table
- On `failed`: "Retry failed step" button — POSTs `/api/ops/kol-network/job/[id]/retry?step=<name>`, server resets that step's status='pending' if `retries < 3`
- On `done`: redirect to `/ops/kol-network/<slug>`

---

## Auth model (Codex Critical #4 — gate every endpoint)

### Edge-safe allowlist module (Codex Maintainability)

Existing `src/lib/allowlist.ts` imports `fs`, `path`, `os` for filesystem fallback — incompatible with edge middleware bundling. Split:

**New file `src/lib/kol-create-allowlist.ts`** (edge-safe, no Node imports):
```ts
export const KOL_CREATE_EMAILS: ReadonlySet<string> = new Set([
  "george@arkn.io",
  "ben@arkn.io",
  "arfcahit1910@gmail.com",
  "siegby@gmail.com",
]);
export function canCreateKolProject(email: string | null | undefined): boolean {
  if (!email) return false;
  return KOL_CREATE_EMAILS.has(email.toLowerCase());
}
```

`middleware.ts` imports from this file; existing `allowlist.ts` re-exports for non-edge consumers.

### Endpoint gates (defense-in-depth on every route)

| Route | Method | Gate logic |
|---|---|---|
| `/ops/kol-network/new` | GET (page) | middleware: redirect if `!canCreateKolProject` · page: re-check via `getSession()`, render 403 |
| `/api/ops/kol-network/preflight` | POST | route handler: `canCreateKolProject` + audit POST |
| `/api/ops/kol-network/create` | POST | route handler: `canCreateKolProject` + per-operator daily quota + audit POST |
| `/api/ops/kol-network/job/[id]` | GET | route handler: `canCreateKolProject(email) && (email == job.submitted_by_email || isAdmin(email))` |
| `/api/ops/kol-network/job/[id]/retry` | POST | same as status, plus audit POST |

**Submitter-or-admin** rule means a non-submitter ops_admin can recover a stuck job for an absent operator without escalating. AGENTS-aligned: existing operator/admin distinction wins.

### Audit table

Every successful AND every denied request to a `/api/ops/kol-network/...` endpoint inserts into `kol_create_audit`:
- `id`, `at_utc`, `email` (nullable — auth_failed may have no session), `endpoint`, `method`, `outcome` (`allowed`/`denied`/`quota_exceeded`/`auth_failed`), `job_id` (nullable), `ip`, `user_agent`

90-day retention via cron-purge — short enough to limit PII exposure, long enough to investigate incidents.

---

## Grok API integration — explicit boundary (Codex Critical #1, round 2)

**Decision: Grok lives in a sidecar container, not on the host and not in SableWeb.** xAI API keys never reach the Node bundle. The service is reachable only over the compose network — never published to the host or the internet.

### Why sidecar, not host-bound + `host.docker.internal`

SableWeb's compose service already declares `extra_hosts: "host.docker.internal:host-gateway"`, but a host process bound to `127.0.0.1` accepts only loopback connections — `host.docker.internal` resolves to the host's bridge gateway, NOT loopback, so the connection is refused. Binding to `0.0.0.0` on the host would work but exposes the xAI key surface to the host's other interfaces unless firewalled. Sidecar is the cleanest pattern: container ↔ container traffic over the compose default network, no firewall to think about, isolated by default.

### Compose changes

Add to `SableWeb/docker-compose.yml`:
```yaml
services:
  sable-kol-preflight:
    image: sable-kol-preflight:latest        # built from SableKOL repo
    restart: unless-stopped
    environment:
      XAI_API_KEY: ${XAI_API_KEY}
      SABLE_SERVICE_TOKEN: ${SABLE_SERVICE_TOKEN}
      SABLE_DATABASE_URL: ${SABLE_DATABASE_URL}
    volumes:
      - /opt/sable-data:/data/db                # for reuse-check DB queries
    # NO `ports:` block — service is reachable only inside the compose network.

  web:
    # ... existing block ...
    environment:
      # ... existing vars ...
      SABLE_KOL_SERVICE_URL: http://sable-kol-preflight:8001
      SABLE_SERVICE_TOKEN: ${SABLE_SERVICE_TOKEN}
    depends_on:
      - sable-kol-preflight
```

### Service surface (`sable_kol/preflight_service.py`, new)

FastAPI bound to `0.0.0.0:8001` *inside the container*:
- `POST /preflight` — `{handle: str}` → enrich response
- `POST /suggest-comparable` — `{handle: str, themes: list[str]}` → comparable handles
- `POST /reuse-check` — `{handles: list[str], freshness_days: int}` → reuse split (NO Grok call, just DB)
- Auth: `X-Sable-Service-Token` header check against `SABLE_SERVICE_TOKEN` env

The `Dockerfile.preflight` lives in the SableKOL repo. CI builds and pushes the image; compose pulls it.

### SableWeb proxies these (server-side only)

`/api/ops/kol-network/preflight/route.ts`:
1. Verify auth (allowlist + audit)
2. `fetch(\`${process.env.SABLE_KOL_SERVICE_URL}/preflight\`, { headers: { "X-Sable-Service-Token": process.env.SABLE_SERVICE_TOKEN }, body: ... })`
3. Validate response against a Zod schema (per SableWeb AGENTS validation-boundary rule)
4. Return to client

### Pydantic schemas (server side)

```python
class PreflightResponse(BaseModel):
    twitter_id: str | None
    handle: str
    bio: str
    followers: int | None
    verified: bool
    is_active: bool
    primary_archetype: Literal["creator", "trader", "developer", "founder", "influencer", "other"]
    primary_sectors: list[str]
    credibility_signal: Literal["high", "medium", "low", "unclear"]
    real_name_known: bool
    listed_count: int | None
    tweets_count: int | None
    following: int | None
    notes: str | None
    recent_themes: list[str]
    audience_archetype: str
    axis_candidates: list[AxisPair]
    signal_metadata: SignalMetadata
```

### Failure modes

| Failure | Handling |
|---|---|
| xAI 5xx | Retry once with 2s backoff; if still failing, `enrich` step marked `failed`, operator can retry from status page |
| xAI 429 | Exponential backoff up to 3 tries; if still failing, defer (mark step `pending`, set `next_retry_at`); next worker run picks it up |
| xAI auth failure | Hard error: log to audit + alerts; service returns 503; SableWeb shows "Grok unavailable, fill manually" UI |
| Pydantic validation failure | Treat as unrecoverable for that step; operator pastes manually |
| Cost ceiling exceeded ($0.50/job) | Mark job `failed` with explicit reason; operator must intervene |

---

## Reuse detection (Codex Data Integrity #2 — dual-driver)

```python
def cohorts_to_fetch(
    db,                       # CompatConnection — dual-driver
    comparison_handles: list[str],
    freshness_days: int = 180,
) -> tuple[list[str], list[str]]:
    """Return (already_have, must_fetch)."""
    cutoff = (now_utc() - timedelta(days=freshness_days)).isoformat()
    norm = [normalize_handle(h) for h in comparison_handles]
    if not norm:
        return [], []
    placeholders = ",".join("?" * len(norm))  # CompatConnection translates `?` positional to SQLAlchemy named params for Postgres
    rows = db.execute(f"""
        SELECT DISTINCT target_handle_normalized
        FROM kol_extract_runs
        WHERE target_handle_normalized IN ({placeholders})
          AND extract_type = 'followers'
          AND cursor_completed = 1
          AND completed_at > ?
    """, (*norm, cutoff)).fetchall()
    already_have = {r[0] for r in rows}
    must_fetch = [h for h in comparison_handles if normalize_handle(h) not in already_have]
    return list(already_have), must_fetch
```

**Why dual-driver:** SablePlatform AGENTS treats SQLite as the local-dev source-of-truth; this function runs in tests against SQLite and on prod against Postgres. No `INTERVAL`, no `ANY()`, no Postgres-specific syntax. ISO-8601 string comparison is lex-correct for date ordering.

**Job claiming (Codex Critical #2, round 2):** SablePlatform's `sable_platform/db/jobs.py` currently has only `create_job`, `add_step`, `start_step`, `complete_step`, `fail_step`, `get_job`, `get_resumable_steps`, `resume_job` — **no atomic claim helper, no job-level running/done transitions, no stale sweeper**. v3 adds them as part of Phase C work, NOT as an assumption. Specifically, this build adds to SablePlatform:

```python
def claim_next_job(
    conn,                       # CompatConnection
    job_type: str,
    worker_id: str,
    stale_after_minutes: int = 10,
) -> dict | None:
    """Atomically claim the oldest pending job of a given type, OR reclaim
    a stale running one (worker_id present but updated_at older than the
    stale threshold — implies a crashed worker).

    Returns None if no claimable job. The returned dict contains the
    job_id + config_json + steps loaded via get_resumable_steps().
    """
    # Postgres path: SELECT ... FOR UPDATE SKIP LOCKED + UPDATE in one tx
    # SQLite path:   BEGIN IMMEDIATE; UPDATE WHERE status='pending' LIMIT 1
    #                with RETURNING (SQLite >= 3.35); commit; if zero rows,
    #                retry the stale-reclaim path.
    # Both paths bump jobs.updated_at AND set jobs.worker_id.
```

This requires **a new column `worker_id TEXT` on `jobs`** — folded into migration 040 (see below). It is generic infrastructure, not KOL-specific; other future job types benefit. We are NOT building a separate `jobs_locks` table.

**Heartbeat / stale-claim recovery**: each step transition bumps `jobs.updated_at` (already happens via `start_step`/`complete_step`/`fail_step`). The stale-reclaim path inside `claim_next_job` looks for `status='running' AND updated_at < (now - stale_after_minutes)` and treats those as claimable. A separate sweeper job is unnecessary because `claim_next_job` is the only entry point.

**Tests for the claim helper (Phase C):**
- Single-worker: claim, work, complete; subsequent claim returns None
- Two simultaneous claimers: exactly one wins (race test, run 100 iterations)
- Crashed worker: claim, set updated_at to 11 minutes ago, second claim succeeds (reclaim)
- Wrong job_type: claim with `job_type='other'` returns None even when KOL jobs are pending
- Both drivers: SQLite + Postgres parity

**Handle rename (Codex edge case)**: reuse-detection matches on `target_handle_normalized` AND `target_user_id` when both are known. Mismatches log to `kol_handle_resolution_conflicts` (existing migration 032 table); the affected handle is treated as un-surveyed for this run; operator sees a warning chip on the status page.

---

## Migration 040 — full SablePlatform AGENTS-compliant spec (Codex Data Integrity #1, expanded round 2)

v3 expands migration 040 to address Codex round-2 findings #4 (missing `next_retry_at`) and #5 (NOT NULL email blocks anonymous-failure audits), plus the `worker_id` column needed by the claim helper.

Per `SablePlatform/AGENTS.md:54`, every schema change requires SQL + `_MIGRATIONS` + `schema.py` + Alembic + parity tests. ALL of the below applies for ALL three changes in this migration.

1. **SQL migration** at `sable_platform/db/migrations/040_kol_wizard_infra.sql`:
   ```sql
   -- Migration 040: KOL wizard infrastructure.
   --
   --   1. kol_create_audit         New append-only auth audit log.
   --                                email is NULLABLE so anonymous /
   --                                unauthenticated failures can still log.
   --   2. jobs.worker_id           New column. Generic — used by the new
   --                                claim_next_job() helper. Not KOL-specific.
   --   3. job_steps.next_retry_at  New column. Set when a step is deferred
   --                                via 429 backoff; worker only attempts
   --                                steps where next_retry_at IS NULL OR
   --                                next_retry_at <= now.

   BEGIN;

   -- (1) Audit table. PII-bearing (submitter email); 90-day retention via cron.
   CREATE TABLE IF NOT EXISTS kol_create_audit (
       id           INTEGER PRIMARY KEY AUTOINCREMENT,
       at_utc       TEXT NOT NULL DEFAULT (datetime('now')),
       email        TEXT,                         -- NULLABLE: auth_failed logs may not have an email
       endpoint     TEXT NOT NULL,
       method       TEXT NOT NULL,
       outcome      TEXT NOT NULL,
       job_id       TEXT REFERENCES jobs(job_id),
       ip           TEXT,
       user_agent   TEXT
   );
   CREATE INDEX IF NOT EXISTS idx_kol_create_audit_email   ON kol_create_audit(email);
   CREATE INDEX IF NOT EXISTS idx_kol_create_audit_at      ON kol_create_audit(at_utc);
   CREATE INDEX IF NOT EXISTS idx_kol_create_audit_outcome ON kol_create_audit(outcome);

   -- (2) Generic worker_id on jobs (not KOL-specific; benefits all future job_types)
   ALTER TABLE jobs ADD COLUMN worker_id TEXT;
   CREATE INDEX IF NOT EXISTS idx_jobs_worker ON jobs(worker_id);

   -- (3) Deferred-retry timestamp on job_steps
   ALTER TABLE job_steps ADD COLUMN next_retry_at TEXT;
   CREATE INDEX IF NOT EXISTS idx_job_steps_next_retry ON job_steps(next_retry_at);

   UPDATE schema_version SET version = 40 WHERE version < 40;

   COMMIT;
   ```

2. **`_MIGRATIONS` entry** in `sable_platform/db/connection.py`:
   ```python
   ("040_kol_wizard_infra.sql", 40),
   ```

3. **`schema.py` declarative entries**:
   - Add `sa.Table("kol_create_audit", ...)` mirroring the SQL (email Column nullable=True)
   - Add `sa.Column("worker_id", sa.Text(), nullable=True)` to the existing `jobs` table definition
   - Add `sa.Column("next_retry_at", sa.Text(), nullable=True)` to the existing `job_steps` table definition

4. **Alembic revision** at `sable_platform/alembic/versions/<hash>_kol_wizard_infra.py`:
   - `upgrade()`: creates table, adds two columns, creates four indexes
   - `downgrade()`: drops indexes, drops `next_retry_at` from `job_steps`, drops `worker_id` from `jobs`, drops `kol_create_audit` table

5. **Parity test** at `tests/db/test_migration_040.py`:
   - Bootstrap fresh SQLite DB, run all migrations, assert all 3 changes landed (table exists, columns exist with correct nullability, indexes exist)
   - Same against temp Postgres DB via Alembic
   - Assert SQL and Alembic produce identical schemas (via `inspect(engine).get_columns()`)
   - **Specifically assert `kol_create_audit.email` is nullable on both drivers**
   - **Specifically assert `jobs.worker_id` and `job_steps.next_retry_at` are nullable on both drivers**

6. **Insert fixture tests** at `tests/db/test_kol_create_audit.py`:
   - Write one row of each `outcome` value (`allowed`, `denied`, `quota_exceeded`, `auth_failed`)
   - Assert `email IS NULL` is accepted for `outcome='auth_failed'` (Codex Critical #5 round 2)
   - Assert FK to `jobs(job_id)` is enforced when `job_id` is set
   - Assert NULL `job_id` succeeds

7. **Claim-helper test fixtures** at `tests/db/test_claim_next_job.py` (covered under Phase C testing above).

**Notably the plan still adds NO `kol_jobs` table.** All wizard job state lives in `jobs` + `job_steps` (with two new columns) + `kol_create_audit`. The three additions in migration 040 are the minimum surface area to support the wizard without altering the `jobs`/`job_steps` contract for other consumers.

---

## Worker model

**systemd timer + existing job-claim helper.** No new locking primitives.

`deploy/jobs/sable-kol-jobs.service`:
- `ExecStart=/opt/sable/venv/bin/sable-kol jobs run --job-type kol_create --max-jobs 1`
- One job per tick (sequential — SocialData rate limits make parallelism counterproductive)

`deploy/jobs/sable-kol-jobs.timer`: 60s interval. RandomizedDelaySec=10s.

### Step machine (state in `job_steps`)

| step_name | retries | resume semantics |
|---|---|---|
| `enrich` | 3 | Idempotent: re-call Grok, overwrite `output_json` |
| `suggest_comparable` | 3 | Idempotent (skipped if step.output_json already set) |
| `reuse_check` | 0 | DB-only, no spend; no point retrying |
| `survey_cohort_<handle>` | 2 | Idempotent: bulk-fetch upsert with conflict ignored |
| `write_yaml` | 0 | Filesystem write; deterministic |
| `regenerate` | 1 | Calls `run_regenerate(client_id)`; classify is the slow step |

**Crash mid-step**: claim is recovered by the stale-claim sweeper (10-min `updated_at` cutoff). The next worker run claims the job, identifies the in-progress step (`status='running'`), and re-attempts that step (counting against `retries`).

**Hard cap**: if any step has `retries >= max_retries`, job marked `failed` with `error` set to the last step's error. Operator clicks "retry" to reset the failed step's status to `pending` (resets retries to 0; AGENTS allows operator override).

### Concurrency

- 1 wizard-job at a time globally (sequential SocialData)
- Existing `jobs` table claim mechanism handles cross-worker safety (already proven in prod)
- The new wizard does NOT race against existing job types because the worker scopes its claim by `job_type='kol_create'`

---

## Cost guardrails

| Lever | Limit | Where |
|---|---|---|
| Grok per-job spend | $0.50 | `enrich`/`suggest_comparable` step internals |
| SocialData per-job spend | $30 | Pre-flight check before each `survey_cohort_<handle>` step |
| Total per-job spend | $50 (hard cap) | Pre-flight check before `survey_cohort_*` steps start |
| Concurrent jobs | 1 globally | `claim_next_job` mechanism |
| Per-operator daily quota | 5 jobs/day | API route check before insert |
| Service-token rotation | every 90 days | Manual; documented in `deploy/SECRETS.md` |

All costs flow through `cost_events` with `job_id` set — Codex Critical #1 fixed by reusing `jobs(job_id)` as FK target.

---

## Build phasing

### Phase A — Foundation (~1.25 days)

- [ ] `src/lib/kol-create-allowlist.ts` (edge-safe, pure data)
- [ ] `src/lib/allowlist.ts` re-exports for non-edge consumers
- [ ] `src/middleware.ts` gates `/ops/kol-network/new`
- [ ] `/ops/kol-network/page.tsx` adds conditional "+ New project" button
- [ ] **Migration 040 (expanded)**: kol_create_audit (email nullable) + jobs.worker_id + job_steps.next_retry_at — SQL + `_MIGRATIONS` + `schema.py` + Alembic + parity test (round-2 fix #4 + #5)
- [ ] Whitney Webb fix already in `filters.py` — ships in this rebuild

**Demo:** button visible to 4 ops, click → 403 stub page (route gated correctly). Migration applied + tested both drivers; assert `kol_create_audit.email`, `jobs.worker_id`, `job_steps.next_retry_at` all nullable on both.

### Phase B — Grok service (sidecar container) + preflight CLI (~1 day, up from half-day post round 2)

- [ ] New `sable_kol/grok_api.py` (pure xAI client)
- [ ] New `sable_kol/preflight_service.py` (FastAPI on `0.0.0.0:8001` — sidecar inside compose net)
- [ ] New `sable-kol preflight <handle>` CLI: prints what the wizard would pre-fill (no DB writes)
- [ ] Pydantic + Zod schemas (server + client validation)
- [ ] **`Dockerfile.preflight`** in SableKOL repo — minimal Python image (round-2 fix #1)
- [ ] **`docker-compose.yml` patch in SableWeb** — adds `sable-kol-preflight` service, sets `SABLE_KOL_SERVICE_URL` on `web` (round-2 fix #1)
- [ ] CI build + push image
- [ ] Tests: mock xAI response, validate schema parsing; one real-world smoke against `@solstitch`

**Demo:** sidecar container running alongside SableWeb in compose; SableWeb route handler successfully reaches it via `SABLE_KOL_SERVICE_URL`; xAI key never appears in SableWeb's environment.

### Phase C — Claim helper + worker + reuse logic (~1.5 days, up from 1 day post round 2)

- [ ] **NEW: `claim_next_job(conn, job_type, worker_id, stale_after_minutes)` in `sable_platform/db/jobs.py`** — dual-driver, atomic, with stale-reclaim path. Tests: single-claim, two-racers, stale reclaim, wrong job_type. (Round-2 fix #2 — this helper does NOT exist today.)
- [ ] `sable_kol/jobs.py`: orchestrator that calls the new `claim_next_job`, walks `job_steps`, persists `output_json`, handles `next_retry_at` deferred-retry path on 429
- [ ] `sable-kol jobs run` CLI subcommand
- [ ] Org auto-create helper: upsert `orgs(org_id, display_name, twitter_handle, status='inactive', config_json={"org_type":"prospect", ...})` (round-2 fix #3)
- [ ] Reuse-detection function (dual-driver via `?` placeholders + ISO-string comparison)
- [ ] systemd timer + service files (note: timer is for the sable-kol-jobs runner, not the preflight container which is Docker-managed)
- [ ] Crash-resume test: kill worker after `survey_cohort_X`, restart, assert no duplicate SocialData fetches
- [ ] Cost-logging test: assert `cost_events.job_id` insert succeeds with the job's UUID

**Demo:** operator manually creates a job via `create_job(...)` + `add_step(...)`, worker `claim_next_job` returns it, runs end-to-end, generates YAML at `/opt/sable/clients/<slug>.yaml`, regenerate produces graph in `/opt/sable/outreach/<slug>/`. Run two workers in parallel, exactly one claims any given job.

### Phase D — Wizard UI + status page (~1 day)

- [ ] `/ops/kol-network/new/page.tsx` — 4-step wizard
- [ ] All `/api/ops/kol-network/...` routes: gated, audit-logged, validated
- [ ] `/ops/kol-network/job/[id]/page.tsx` — status page with step-by-step view
- [ ] AI-assisted chips + freshness timestamps on Grok-derived fields
- [ ] Reuse preview live debounce
- [ ] Daily-quota check
- [ ] Tests: all required tests below

**Demo:** end-to-end. Operator clicks button, fills wizard, watches status, lands on the new project's network page when ready.

### Total: ~4-5 days post round-2 (up from 3-4), four shippable demos. Bulk of the increase is the genuine claim helper + sidecar Dockerization Codex flagged were missing.

---

## Required tests (Codex)

1. **Migration parity** (`tests/db/test_migration_040.py`):
   - Run all migrations on a fresh SQLite DB; assert `kol_create_audit` exists with expected columns + indexes
   - Run via Alembic on a temp Postgres DB; assert same shape
   - Assert SQLite + Alembic produce identical column lists (`inspect`)

2. **Cost-logging FK** (`tests/test_cost_events_kol_job.py`):
   - Insert an `orgs` row, a `jobs` row with `job_type='kol_create'`, a `cost_events` row referencing that job
   - Assert insert succeeds with no FK violation
   - Assert FK violation fires when `job_id` references a non-existent UUID

3. **Worker resume idempotency** (`tests/test_jobs_resume.py`):
   - Set up a job with one cohort survey already in `kol_extract_runs`, one un-surveyed
   - Run worker, assert: 1 SocialData fetch, not 2
   - Kill worker after the fetch, before `regenerate`
   - Re-run worker, assert: 0 SocialData fetches, regenerate completes

4. **SableWeb auth** (`SableWeb/tests/api-kol-network-auth.test.ts`):
   - Non-allowlisted email gets 403 on POST `/preflight`, POST `/create`, GET `/job/[id]`, POST `/job/[id]/retry`
   - Allowlisted submitter can read their own job; non-submitter allowlisted ops can also read (admin override)
   - Audit row inserted on every endpoint hit (allowed AND denied)
   - **Anonymous (no session) hit on any endpoint returns 401, inserts an `auth_failed` row with `email IS NULL`** (Codex Critical #5 round 2)

5. **End-to-end visibility** (`SableWeb/tests/integration-kol-wizard.test.ts`):
   - Mock the SableKOL service; submit a wizard request; assert a row in `jobs`
   - Mock the worker output: write a YAML at `/opt/sable/clients/<slug>.yaml` and a network JSON at `/opt/sable/outreach/<slug>/latest_network_interactive.json`
   - Assert `discoveredClientIds()` returns the new slug
   - Assert `/ops/kol-network/<slug>` renders with non-empty data

---

## Open questions for Codex (round 2)

1. **Axis library — fixed or free-form?** v2 starts fixed (10 labels listed in Step 2). Operator can request additions via Slack but cannot type custom values. Trade-off: comparable across projects vs expressive. Codex's call.

2. **Org auto-create at submit time** — is `org_type='prospect'`, `is_active=False` the right default, or should the wizard prompt for org metadata? v2 defaults to silent prospect; flag any concerns.

3. **Service-token threat model** — the `SABLE_SERVICE_TOKEN` shared between SableWeb and the SableKOL preflight service is a long-lived secret. Should we use signed JWTs with short TTL instead? Trade-off: rotation operations vs blast radius. v2 says shared secret, manual rotation; flag if that's wrong.

4. **`kol_create_audit` retention** — 90 days proposed. Too short (incident investigations may need 1+ year)? Too long (PII)? Should we hash the email at write time and store only the hash for older rows?

5. **Should the wizard support "preflight without commit"** — i.e., let the operator dry-run a handle, see what Grok suggests, decide NOT to proceed, with no audit row marking them as having submitted? v2 logs preflight but doesn't insert `jobs`, so a dry-run leaves only an audit trail. Acceptable?

6. **Notification on completion** — v2 says status page only; no email/Slack. Codex: is in-app status sufficient for an ops tool, or does this need a Slack webhook to `#ops`?

---

## What this plan deliberately does NOT solve

- Generalized cross-client `cohort_pool` abstraction — the architecturally clean fix. Reuse-detection here is per-survey, not per-cohort. (See `project_sablekol_graph_reuse.md` for the full framing.)
- Decay strategy — 180-day window is one global knob, not per-vertical.
- Cancel running job button — operator must wait or SSH.
- Cross-operator job visibility — siloed by submitter except for admin override.
- Bulk job operations.

---

## Codex audit response — round 1 (resolved in v2)

| Codex finding | v2 response |
|---|---|
| **Critical #1**: cost_events FK to jobs | Reuse `jobs` + `job_steps`; no `kol_jobs` table. Cost FK works as designed. |
| **Critical #2**: stale paths | Switch to `client_config.PROD_CLIENT_DIR` + `outreach_output_dir()`; never hard-code `~/.sable`. |
| **Critical #3**: Grok boundary | Explicit FastAPI service in SableKOL, service-token auth, SableWeb proxies server-side only. |
| **Critical #4**: auth too narrow | Every endpoint gated; submitter-or-admin rule; audit on every hit. |
| **Data #1**: migration spec | Full SQL + `_MIGRATIONS` + `schema.py` + Alembic + parity test in Phase A. |
| **Data #2**: Postgres-only worker | Reuse-check uses `IN (...)` + ISO-string comparison. |
| **Data #3**: retry/resume schema | Reuse `job_steps.retries`, `job_steps.error`, `job_steps.output_json` checkpoints. |
| **Maintainability #1**: edge-unsafe allowlist | New `kol-create-allowlist.ts` pure-data module imported by middleware. |
| **Maintainability #2**: missing kol_create_audit | Added to migration 040 spec with retention policy. |
| **Maintainability #3**: AI-signal labeling | `signal_metadata` block on every Grok response; AI-assisted chips + freshness timestamps in wizard UI. |

## Codex audit response — round 2 (resolved in v3)

| Codex round-2 finding | v3 response |
|---|---|
| **Blocker #1**: SableWeb cannot reach `127.0.0.1` from Docker | Made the preflight a **sidecar container** in the same compose network (no published port, internal-only). SableWeb reaches it via `SABLE_KOL_SERVICE_URL` env. New `Dockerfile.preflight` in SableKOL repo. xAI key only in the sidecar's environment, never in SableWeb's. |
| **Blocker #2**: `claim_next_job` doesn't exist | Phase C now explicitly **adds it** to `sable_platform/db/jobs.py` as part of the build, with a dual-driver implementation (Postgres `FOR UPDATE SKIP LOCKED`, SQLite `BEGIN IMMEDIATE` + `RETURNING`), stale-reclaim path, and a 4-case test suite. |
| **Blocker #3**: orgs schema mismatch (`org_type`, `is_active` don't exist) | Org auto-create now uses `status='inactive'` + `config_json={"org_type":"prospect", ...}` — matches the actual `orgs` schema (`org_id, display_name, discord_server_id, twitter_handle, config_json, status`). |
| **Blocker #4**: `next_retry_at` not in `job_steps` | Migration 040 expanded to add `job_steps.next_retry_at TEXT` (nullable) + an index. |
| **Blocker #5**: audit can't log anonymous failures (NOT NULL email) | Migration 040 expanded: `kol_create_audit.email` is **NULLABLE**; the round-2 fix is enforced as a parity-test assertion on both drivers. |
| Polish: SableWeb tests live in `tests/`, not `__tests__` | Test paths corrected throughout. |
| Polish: CompatConnection comment is wrong | Comment now correctly reads "translates `?` positional to SQLAlchemy named params for Postgres". |

**Net additions from round 2:** sidecar Dockerization (~half day), claim helper + tests (~half day), two more columns in migration 040 (no extra time, same migration). Total build estimate moved from 3-4 days → 4-5 days.

**What v3 does NOT change from v2:** the core architectural decisions (reuse `jobs`/`job_steps`, dual-driver everywhere, paths via `client_config` resolvers, every endpoint gated + audited, signal_metadata on AI outputs). Round-2 issues were all about implementation surface, not architecture.

---

## Memory / context references for Codex round 2

- `~/.sable/clients/solstitch.yaml` — canonical wizard output example
- `~/Projects/SableKOL/sable_kol/regenerate.py:run_regenerate()` — worker invokes this
- `~/Projects/SableKOL/sable_kol/grok_import.py` — Grok JSON parser (already exists)
- `~/Projects/SableKOL/sable_kol/client_config.py:PROD_CLIENT_DIR` + `outreach_output_dir()` — path resolvers
- `~/Projects/SablePlatform/sable_platform/db/migrations/001_initial.sql:115` — `jobs` + `job_steps` schema
- `~/Projects/SablePlatform/sable_platform/db/migrations/001_initial.sql:155` — `cost_events` schema
- `~/Projects/SablePlatform/sable_platform/db/connection.py:_MIGRATIONS` — migration registry
- `~/Projects/SableWeb/src/lib/client-config.ts` — `discoveredClientIds()` and config loader
- `~/Projects/SableWeb/src/lib/kol-network-data.ts` — `loadKOLNetwork()`
- `~/Projects/SableWeb/AGENTS.md:23,47` — boundaries and signal-type taxonomy
- `~/Projects/SablePlatform/AGENTS.md:54` — migration requirements
- Memory file `project_sablekol_graph_reuse.md` — broader graph-reuse framing
- The 4-operator allowlist is intentionally tighter than the existing ops allowlist; do not relax without explicit Sieggy approval
