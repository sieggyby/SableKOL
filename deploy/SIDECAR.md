# Preflight sidecar — build, deploy, rotate

The any-project KOL wizard's "preflight" step (Phase B) is served by a small
FastAPI app shipped as a Docker sidecar that runs alongside SableWeb. xAI keys
never reach the Node bundle — the sidecar is the only container that talks to
api.x.ai.

This doc is the operator runbook. The architectural rationale is in
`docs/any_project_wizard_plan.md` (v3); the implementation surface is in
`sable_kol/preflight_service.py` + `sable_kol/grok_api.py`.

---

## Layout

The build expects this on-disk layout (matches the prod box's `/opt/sable`):

```
<parent>/
├── SableKOL/
│   ├── Dockerfile.preflight
│   ├── pyproject.toml
│   └── sable_kol/...
└── SablePlatform/
    └── pyproject.toml
```

`SablePlatform` is a hard dep (editable-installed inside the image). The build
context is the parent directory, NOT SableKOL itself.

---

## Build

From the parent directory of both repos (e.g. `/opt/sable` on prod,
`~/Projects` on a laptop):

```bash
docker build \
    -f SableKOL/Dockerfile.preflight \
    -t sable-kol-preflight:latest \
    .
```

That tag is what `SableWeb/docker-compose.yml` references. No registry, no
push. v1 keeps the image local on whichever box runs SableWeb's compose
stack.

If we ever multi-host: swap to GHCR by adding a `docker push` step and
updating the `image:` line. Out of scope for v1.

---

## Required env vars

These belong in `/opt/sable/.env` on prod (loaded by `docker-compose`):

| Var | Where it lives | Purpose |
|---|---|---|
| `XAI_API_KEY` | sidecar only | xAI Grok API key. Hard-fails on missing. |
| `SABLE_SERVICE_TOKEN` | both sidecar AND SableWeb | Shared token. Generate with `openssl rand -hex 32`. Rotate every 90 days. |
| `SABLE_DATABASE_URL` | both | Postgres URL for `kol_extract_runs` reads (the `/reuse-check` query). |

The sidecar reads `SABLE_DB_PATH` only as a fallback if `SABLE_DATABASE_URL`
is unset (dev / test mode against SQLite).

`SABLE_KOL_SERVICE_URL` and `SABLE_SERVICE_TOKEN` are set on SableWeb's `web`
service so its API routes can reach the sidecar via the compose network at
`http://sable-kol-preflight:8001`.

---

## First deploy

After Phase A is on prod (migration 040 + alembic upgrade head + SableKOL
pull + SableWeb pull):

1. SSH to the prod box.
2. Generate the service token: `openssl rand -hex 32`. Append to
   `/opt/sable/.env`:
   ```
   SABLE_SERVICE_TOKEN=<hex>
   XAI_API_KEY=<from-xai-dashboard>
   ```
3. Pull SableKOL: `cd /opt/sable/SableKOL && git pull`.
4. Pull SableWeb: `cd /opt/sable/SableWeb && git pull`.
5. Build the sidecar image:
   ```bash
   cd /opt/sable
   docker build -f SableKOL/Dockerfile.preflight -t sable-kol-preflight:latest .
   ```
6. Bring up the new compose stack:
   ```bash
   cd /opt/sable/SableWeb
   docker-compose up -d
   ```
   Compose's `depends_on: condition: service_healthy` blocks `web` from
   starting until the sidecar's `/healthz` returns 200.
7. Smoke-test from inside the SableWeb network:
   ```bash
   docker-compose exec web wget -qO- \
       --header "X-Sable-Service-Token: $SABLE_SERVICE_TOKEN" \
       --post-data '{"handle": "solstitch"}' \
       --header "Content-Type: application/json" \
       http://sable-kol-preflight:8001/preflight
   ```
   Expect a JSON body with `"signal_metadata": {"source": "grok_xai_live", ...}`.

---

## Token rotation (every 90 days)

Both ends must rotate atomically:

1. Generate the new token: `openssl rand -hex 32`.
2. Update `/opt/sable/.env` with the new value (overwrite the old one).
3. `docker-compose up -d` — both `web` and `sable-kol-preflight` restart with
   the new token. Brief outage (~5s) on the wizard endpoint during rollover;
   acceptable for an ops-only tool.
4. Log the rotation in `deploy/SECRETS.md` (or wherever the existing rotation
   log lives). Include date + operator email.

---

## Rebuild on code change

The image embeds the SableKOL + SablePlatform source. Any code change requires
a rebuild:

```bash
cd /opt/sable
docker build -f SableKOL/Dockerfile.preflight -t sable-kol-preflight:latest .
cd SableWeb
docker-compose up -d sable-kol-preflight
```

The `web` container does not need to restart unless its compose block changed.

---

## Troubleshooting

| Symptom | Likely cause | Check |
|---|---|---|
| `web` health stuck | sidecar `/healthz` failing | `docker-compose logs sable-kol-preflight` |
| `503 service token not configured` | `SABLE_SERVICE_TOKEN` unset on sidecar | `docker-compose exec sable-kol-preflight env \| grep SABLE_SERVICE_TOKEN` |
| `503 xAI auth failure` | `XAI_API_KEY` unset or invalid | Verify against xAI dashboard; rotate if leaked |
| `403 invalid or missing service token` | SableWeb and sidecar have different tokens | Compare `SABLE_SERVICE_TOKEN` on both containers |
| `502 xAI returned an unparseable response` | Grok schema drift | Check `grok-4-latest` release notes; pin to a specific snapshot if recurring |
| `/reuse-check` returns empty `must_fetch` | DB connection wrong or `kol_extract_runs` empty | `docker-compose exec sable-kol-preflight python -c "from sable_kol.db import open_db; \nwith open_db() as c: print(c.execute('SELECT COUNT(*) FROM kol_extract_runs').fetchone())"` |

---

## Manual smoke against live xAI (NOT in CI)

CI-blocked smoke tests would bill xAI on every run. The live `@solstitch`
smoke is operator-triggered:

```bash
cd /Users/sieggy/Projects/SableKOL
.venv/bin/sable-kol preflight solstitch
```

Expects a JSON dump that includes `axis_candidates` from the fixed library
and 8-10 `comparable_projects`. Run before each prod cut to catch xAI breakage
early.

---

## /draft-intro (KO-3) — per-candidate cold-intro drafter

`POST /draft-intro` returns a 2-3 line operator-flavored opener for a single
candidate. Token-gated like the other endpoints; fails 422 on invalid persona
or unwhitelisted `candidate_signal` fields, 409 on the `ben` placeholder, 502
on xAI failure.

| Endpoint | Status | Detail |
|---|---|---|
| 200 | success | `{intro_text, suggested_angle, signal_metadata}` |
| 403 | bad token | shared token mismatch |
| 409 | persona placeholder | currently only `ben` |
| 422 | bad request | invalid persona, unwhitelisted candidate_signal key, or oversized free-text |
| 502 | xAI failure | unparseable Grok response or upstream 5xx after retry |
| 503 | xAI auth failure | `XAI_API_KEY` rejected |

The audit ledger and per-operator quota live entirely on SableWeb's side at
`/api/ops/kol-network/[clientId]/draft-intro`; the sidecar has no quota or
audit logic. **Drafts are NOT persisted server-side** — the response is
ephemeral and the operator copy-pastes the text.

Cost ceiling: ~$0.005-0.01/call expected, 50 attempts/operator/24h cap →
~$0.50/operator/day, ~$2.00/day worst-case across the four KOL allowlist
operators (siegby, george, arf, ben).

Manual smoke from the host venv:

```bash
cd /Users/sieggy/Projects/SableKOL
.venv/bin/sable-kol draft-intro alice \
    --persona sieggy \
    --project-context "TIG: DeAI bounty IP on Base" \
    --bio "convex optimization, occasional crypto curiosity" \
    --archetype researcher \
    --top-signal "tier B" --top-signal "cluster: research-academic" \
    --tier B
```

`sable-kol persona-manifest --json` emits the canonical operator-persona
slug list — SableWeb's `tests/fixtures/persona_manifest.json` is regenerated
from this in CI to keep the TS persona union in lockstep.

---

## Forced regenerate (KO-3 Phase 4)

The `_meta.generated_at_utc` timestamp added at
`scripts/build_outreach_plan.py:443` is only present on outreach plans
generated post-deploy. The `/draft-intro` SableWeb route falls back to file
mtime for older files (flagged `approximate=true` on the response), but a
forced regenerate ensures every prod client lands a canonical timestamp:

```bash
# On the prod box, after pulling the KO-3 commit:
for client in $(ls /opt/sable/clients | sed 's/\.yaml$//'); do
  cd /opt/sable/SableKOL && \
    .venv/bin/sable-kol regenerate "$client"
done
```

Run once per deploy. A subsequent draft-intro request against any client
should return `input_freshness.approximate=false`.
