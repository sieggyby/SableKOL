# Hetzner deploy — KOL viewer (build-order item 10)

**Status:** documentation only. No prod changes without operator (`sieggy`) approval.
**Target host:** Hetzner CX21 (`sable.tools`). Existing services: `sable-web`, `sable-serve`, `cloudflared`, Postgres.
**Companion:** `sableweb_kol_build_plan.md` (overall plan), `~/Projects/SableKOL/deploy/regenerate/` (systemd timers).

---

## What needs to land on the box

1. **SableKOL CLI install** — repo + venv at `/opt/sable/sable-kol/`, editable-installed against the production SablePlatform install. Operator already runs Slopper from `/opt/sable/slopper/` per existing `deploy/DEPLOY.md` patterns; SableKOL slots in the same way.
2. **Per-client config dir** — `/opt/sable/clients/<id>.yaml` (read-only mount inside the SableWeb Docker container; read/write for the SableKOL CLI on the host).
3. **Outreach artifact dir** — `/opt/sable/outreach/<client>/` (read-only mount inside SableWeb; read/write for the regenerate systemd service).
4. **systemd timers** — `sable-kol-regenerate@<client>.timer` (already authored at `~/Projects/SableKOL/deploy/regenerate/`).
5. **SableWeb deps refresh** — `@react-pdf/renderer` and `yaml` (already in `package.json`; just needs `npm ci` / Docker rebuild).
6. **`docker-compose.yml` volume mounts** — append two read-only volumes to the `web` service.
7. **Postgres migrations 038 + 039** — apply via Alembic (SablePlatform handles this) before the regenerate timer fires for the first time.

---

## Filesystem layout (target)

```
/opt/sable/
├── sable-kol/                              # editable git checkout
│   ├── .venv/                               # venv with sable-kol installed
│   ├── sable_kol/...
│   └── scripts/
├── clients/                                # per-client YAML (rw for CLI, ro for web container)
│   ├── solstitch.yaml
│   ├── tig.yaml         (future)
│   └── multisynq.yaml   (future)
└── outreach/                               # generated artifacts (rw for cron, ro for web)
    └── solstitch/
        ├── solstitch_report_2026-05-07_stealth.{md,json}
        ├── solstitch_report_2026-05-07_stealth_full.{md,json}
        ├── solstitch_leads_2026-05-07_stealth.{json,csv}
        ├── solstitch_leads_2026-05-07_stealth_full.{json,csv}
        ├── solstitch_network_2026_05_07_interactive.{gexf,html,json}
        ├── latest_stealth_report.{md,json}                 (symlinks)
        ├── latest_stealth_report_full.{md,json}
        ├── latest_stealth_leads.{json,csv}
        ├── latest_stealth_leads_full.{json,csv}
        └── latest_network_interactive.json
```

**Ownership:** `sable:sable` (the existing service user). The SableWeb Docker container reads via mount; it does not need to own the files.

---

## Step-by-step deploy

### 1. Pre-flight (one-shot)

```bash
ssh sable.tools
sudo mkdir -p /opt/sable/clients /opt/sable/outreach
sudo chown -R sable:sable /opt/sable/clients /opt/sable/outreach

# Postgres migrations 038 + 039 (idempotent — re-runs are safe).
sudo -u sable bash -lc 'cd /opt/sable/platform && .venv/bin/alembic upgrade head'

# Install / pull SableKOL.
cd /opt/sable
sudo -u sable git clone https://github.com/sieggyby/SableKOL.git sable-kol  # one-time
cd sable-kol
sudo -u sable .venv/bin/pip install -e '.[paid-enrich]'
```

**Expected:** Alembic shows migration `c7d9e5f6a039` applied. `sable-kol --version` returns the installed version.

### 2. Place the per-client config

For SolStitch first (other clients later as they onboard):

```bash
sudo -u sable cp ~/.sable/clients/solstitch.yaml /opt/sable/clients/solstitch.yaml
sudo -u sable chmod 0644 /opt/sable/clients/solstitch.yaml
```

Verify validation:

```bash
sudo -u sable /opt/sable/sable-kol/.venv/bin/python -c "
from sable_kol.client_config import load_client_config
c = load_client_config('solstitch')
print(c.client_id, c.mode, len(c.themes), 'themes')
"
```

### 3. First regenerate (manual)

Validates the whole pipeline before handing it over to systemd:

```bash
sudo -u sable /opt/sable/sable-kol/.venv/bin/sable-kol regenerate solstitch --skip-classify --json
```

**Expected:** JSON summary, `errors: []`, files appearing under `/opt/sable/outreach/solstitch/`. If classify is skipped, total time is ~5-10s. With classify (the systemd-timer mode), allow 10-30 minutes.

### 4. Install the regenerate timer

```bash
sudo cp /opt/sable/sable-kol/deploy/regenerate/sable-kol-regenerate@.service \
        /etc/systemd/system/
sudo cp /opt/sable/sable-kol/deploy/regenerate/sable-kol-regenerate@.timer \
        /etc/systemd/system/

# Edit the .service file to set ANTHROPIC_API_KEY and SABLE_DATABASE_URL
# (TODO: switch to a sourced EnvironmentFile= once secrets management exists)
sudo $EDITOR /etc/systemd/system/sable-kol-regenerate@.service

sudo systemctl daemon-reload
sudo systemctl enable --now sable-kol-regenerate@solstitch.timer

# Verify
systemctl list-timers | grep sable-kol-regenerate
sudo systemctl start sable-kol-regenerate@solstitch.service     # one-off run, optional
journalctl -u sable-kol-regenerate@solstitch.service -f         # tail
```

### 5. Update SableWeb's docker-compose

Append to `services.web.volumes` in `/opt/sable/sable-web/docker-compose.yml`:

```yaml
services:
  web:
    volumes:
      # ... existing entries ...
      - /opt/sable/clients:/sable/clients:ro
      - /opt/sable/outreach:/sable/outreach:ro
```

These mounts back the path resolution in `src/lib/client-config.ts` and `src/lib/outreach-files.ts` (both default to `/sable/clients` and `/sable/outreach` in production).

### 6. Rebuild and restart SableWeb

```bash
cd /opt/sable/sable-web
sudo -u sable git pull
sudo -u sable docker compose build web
sudo -u sable docker compose up -d web
sudo -u sable docker compose logs -f web
```

**Expected:** healthcheck passes within ~30s. New routes resolve at:

```
https://sable.tools/ops/kol-network/solstitch
https://sable.tools/api/ops/kol-network/solstitch/report.pdf
https://sable.tools/api/ops/kol-network/solstitch/tags
```

---

## Smoke tests (post-deploy)

Run these from the operator laptop while authenticated to `sable.tools`. Skip the page test if Google OAuth is in the way; just hit the API.

```bash
# 1. Page renders, lists SolStitch
curl -s -b "sable_session=$SESSION" \
  https://sable.tools/ops/kol-network/solstitch | grep -oE 'SolStitch|stealth|axis_scores' | sort -u

# 2. Downloads — all 5 file types
for kind in report.json report.md leads.json leads.csv report.pdf; do
  HTTP=$(curl -s -o "/tmp/${kind}" -w "%{http_code}" -b "sable_session=$SESSION" \
    "https://sable.tools/api/ops/kol-network/solstitch/${kind}")
  echo "${kind}: HTTP ${HTTP}, $(wc -c < /tmp/${kind}) bytes"
done

# 3. PDF magic bytes
head -c 4 /tmp/report.pdf | xxd      # expect: %PDF

# 4. Include flag truth-table (Codex P1-2)
for q in "" "?include_orgs=1" "?include_celebs=1" "?include_orgs=1&include_celebs=1"; do
  N=$(curl -s -b "sable_session=$SESSION" \
    "https://sable.tools/api/ops/kol-network/solstitch/leads.json${q}" \
    | python3 -c "import json,sys; d=json.load(sys.stdin); print(len(d['leads']))")
  echo "${q:-default}: ${N} leads"
done

# 5. Tag write
curl -s -b "sable_session=$SESSION" -X POST \
  -H "content-type: application/json" \
  -d '{"handle":"loomdart","status":"relationship","note":"smoke-test post-deploy"}' \
  https://sable.tools/api/ops/kol-network/solstitch/tags

# 6. Tag read
curl -s -b "sable_session=$SESSION" \
  https://sable.tools/api/ops/kol-network/solstitch/tags | jq '.count'
```

**Pass criteria:**
- All five downloads return HTTP 200 with non-zero bytes.
- PDF starts with `%PDF`.
- Include flag totals are monotonic: default ≤ either-flag ≤ both-flags.
- Tag write returns 201; subsequent read shows count ≥ 1.

---

## Rollback

If something breaks after deploy and the operator wants to back out:

```bash
# 1. Stop the timer (kills future regenerates immediately).
sudo systemctl disable --now sable-kol-regenerate@solstitch.timer

# 2. Roll back SableWeb to previous tag.
cd /opt/sable/sable-web
sudo -u sable git log --oneline -5     # find prior good commit
sudo -u sable git checkout <prior-tag>
sudo -u sable docker compose build web && docker compose up -d web

# 3. Migrations 038 + 039 are append-only and don't break older code reading
#    those tables (the columns/tables just go unused). No DDL rollback needed
#    unless you specifically want to drop them via `alembic downgrade`.
```

The `/opt/sable/outreach/` files are immutable dated artifacts — no need to clean them up on rollback. The `latest_*` symlinks resolve to whatever was newest at the time.

---

## Known unknowns

* **Google OAuth + page render in CI / smoke** — the smoke-test commands above use a session cookie. The CI deploy script in `sable-web/deploy/smoke-test.sh` will need either an admin-token bypass or a Playwright-style auth flow. Out of scope for this build-order; flagged for the next deploy iteration.
* **PDF generation memory** — `@react-pdf/renderer` allocates ~50-100MB for a 250-row plan. Hetzner CX21 has 4GB RAM; comfortable. If we ever generate giant plans (1000+ rows), monitor and consider chunking the page.
* **Postgres connection pool** — SableWeb opens a `pg` pool per Lambda-style request. The new tag routes do one read + (for POST) one write. If the route count balloons, plan to add per-route connection limits.
* **Concurrent operator writes** — last-write-wins on the same `(handle, client_id)` tuple. The append-only history means earlier writes are still queryable, but a colleague could overwrite your status without seeing it. Acceptable for v1; the Phase-2 dedicated `/tagged` table view (build plan "out of scope") would surface concurrent-edit warnings.
* **Backups** — `kol_operator_relationships` is append-only and small (≤140K rows lifetime per the build plan estimate). Already covered by the existing `pg_dump` cron in `deploy/DEPLOY.md`. No new backup work needed.

---

## What this build does NOT include

Defer-to-Phase-2 items per the build plan, repeated here so the operator isn't surprised post-deploy:

* No `/regenerate` web button — refresh is cron + SSH only (audit #1).
* No saved views (named filter states).
* No `/ops/kol-network/[clientId]/tagged` table view of all tagged KOLs.
* No cross-client compare (side-by-side two networks).
* No bulk tagging via multi-select.
* No client-facing redacted view.
* No websocket-pushed tag updates (5-min ISR is the SLA).
* No admin UI for editing per-client YAML (operator SSH-edits files).
