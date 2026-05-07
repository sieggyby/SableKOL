# SableWeb KOL viewer — build plan

**Date:** 2026-05-07
**Status:** Implementer-ready. Ships after SableKOL generalization Phase 2 (per-client YAML config) lands.
**Source:** /grill-me session (Q1-Q12); design doc at `sableweb_kol_network_plan.md`.

---

## What this ships

A single page at `/ops/kol-network/[clientId]` that:

1. **Renders the KOL follow-graph** as a 2D semantic-axis layout (x = client-thesis-relevance, y = crypto-native-ness), with all interactive controls
2. **Generates downloads** — Markdown, PDF, JSON (outreach-plan-shaped + flat leads), CSV
3. **Captures operator tagging** of KOLs (7-status enum, shared across operators on the same client)

Lives under `/ops` — operator-only, never client-visible.

---

## Locked decisions (from grill)

| # | Decision |
|---|---|
| Q1 | 2D layout (not 3D) |
| Q2 | Two semantic axes per client (generic field names `axis_scores.x` / `axis_scores.y`). SolStitch axes are labelled "fashion" (x) and "crypto-native" (y) via `_meta.network_axes.{x,y}.label`. Per-client YAML defines keyword sets and labels |
| Q3 | Node size = `4 + 4 × log₁₀(followers)^1.7`, soft-capped at 80px, multiplied by panel slider |
| Q4 | Pure semantic position + d3-force `forceCollide` only — NO edge attraction |
| Q5 | Score = count-of-matched-tokens / saturation (default 4); +0.5 archetype boost (`trader/anon` → crypto, `artist/creator` → x-axis) |
| Q6 | Live-mode (Hetzner Postgres) in one shot — no Vercel fixture-mode demo |
| Q7 | Tags shared across operators on the same client by default. **`is_private` flags only the `note` text — status is always shared** (operator coordination requires this). Audit finding #3 |
| Q8 | Tag enum: `dm_sent / replied / replied_engaged / meeting / relationship / pass / blocked` (7 values) |
| Q9 | Per-client YAML at `/opt/sable/clients/<id>.yaml` (file-backed, not DB) |
| Q10 | PDF via `@react-pdf/renderer` (server-side) |
| Q11 | Python (SableKOL) writes canonical `.md/.json/.csv` to `/opt/sable/outreach/<client>/`; SableWeb reads the file then **merges live tag state from Postgres at request time** before streaming the response. PDF rendered on demand. Audit finding #2 |
| Q12 | Tag UX = right-side panel on node click + small badge on node + color-mode toggle |

Plus from the original batch (also locked, no override):

* Outreach plan only for v1 (not network screenshot)
* Filenames on disk include a `<doc>` discriminator since two documents share the run: `<doc> ∈ {report, leads}`. Pattern: `{client_id}_{doc}_{YYYY-MM-DD}_{mode}.{ext}`. Examples: `solstitch_report_2026-05-07_stealth.md`, `solstitch_leads_2026-05-07_stealth.csv`. Plus `_full` variants per the include-flags rule. Symlinks: `latest_{mode}_{doc}.{ext}` (e.g. `latest_stealth_report.md`, `latest_stealth_leads.json`). API routes serve `latest_<mode>_<doc>` by default; `?date=2026-05-07` selects a specific dated file. Audit finding #6 + Codex P1-1
* Ops-only auth (`isOpsRole(session.role)`)
* Two JSON shapes: `report.json` (grouped by tier, mirrors current Python output) and `leads.json` (flat array, one per candidate)
* JSON default-filters orgs+celebs. **Python writes BOTH a filtered file (`*_<doc>_*`) and an unfiltered file (`*_<doc>_*_full`).** When `include_orgs=1` AND/OR `include_celebs=1`, the API loads the `_full` variant and applies whichever filter the operator did NOT request to disable, in TypeScript via `src/lib/kol-filters.ts` (port of the Python predicates). This means setting `include_orgs=1` alone keeps celebrities filtered out; setting both flags returns the raw `_full` file. Audit finding #7 + Codex P1-2 (avoids the four-variants-on-disk explosion)
* No pagination (under 5K rows)
* `_meta.schema_version: 1` field
* `kol_operator_relationships` is append-only history. **Current-state query is deterministic: `ORDER BY created_at DESC, id DESC LIMIT 1`** per `(handle_normalized, client_id)` — SQLite `datetime('now')` is second-resolution so `id` is the tiebreaker. Add covering index `(client_id, handle_normalized, created_at DESC, id DESC)`. Audit finding #5
* Tags propagate to deliverables — PDF gets a "Status" column, leads.json includes `operator_relationship`
* Path-param URL: `/ops/kol-network/[clientId]`
* Header dropdown picker, sourced from `/opt/sable/clients/*.yaml`
* Permalinks via named query params (`?size=less&roles=cand,cohort&...`); NO full-state JSON encoding
* Saved views, cross-client compare, dedicated `/tagged` table view = Phase 2 (out of v1 scope)
* No websockets — optimistic UI + 5-min ISR cache
* **No spend kicked from UI.** Refresh runs via Hetzner systemd timer (daily 03:00 UTC). Operators run `sable-kol regenerate <client>` over SSH for emergencies. The `/regenerate` web route is **out of v1 scope** (would need: writable mount, advisory lock, budget guard, admin role). Audit finding #1

---

## Prerequisites (must land before this build)

1. **SableKOL generalization Phase 2** — per-client YAML config at `~/.sable/clients/<id>.yaml` (operator laptop) and `/opt/sable/clients/<id>.yaml` (Hetzner). Schema includes `network_axes.{x,y}.{label, keywords, saturation, archetype_boosts}`, `audiences[].handle`, `cohort_handles[]`, `themes`, `manual_pins`, `tier_thresholds.{stealth,public}`. SolStitch config first.
2. **`scripts/build_outreach_plan.py` writes to configured path** — change from `~/Downloads/` to `--output-dir` (defaults to `/opt/sable/outreach/<client_id>/` in production, `~/Downloads/` for local). Filename pattern: `{client}_outreach_plan_{YYYY-MM-DD}_{mode}.{ext}`. Also writes a `latest_{mode}.{ext}` symlink. Stale dated files ARE retained for history (don't auto-prune).
3. **`scripts/build_outreach_plan.py` writes both filtered and `_full` variants** — to satisfy `?include_orgs=1&include_celebs=1` query-param overrides. Two file pairs per run: `<...>_<mode>.{ext}` (filtered, default) and `<...>_<mode>_full.{ext}` (unfiltered).
4. **`scripts/build_outreach_plan.py` also writes `leads.json` + `leads.csv`** — flat array shape, one row per candidate, all per-row fields. Same generator, additional outputs (filtered + `_full`).
5. **SablePlatform migration 038** — `kol_operator_relationships` table (this build plan's first step). DONE.
6. **SablePlatform migration 039** — `kol_extract_runs.client_id` column (default `'_external'`, backfill `'solstitch'` for the existing SolStitch runs). Required so `loadKOLNetwork(clientId)` can scope edges/nodes per client. **Audit finding #4** — without this column, multi-client graphs bleed together.
7. **Outreach JSON shape uses generic axis fields** — `axis_scores.x`, `axis_scores.y` (with labels in `_meta.network_axes`), NOT `fashion_score / crypto_score`. Audit finding #8.

---

## Client → graph mapping (audit finding #4)

`loadKOLNetwork(clientId)` resolves the per-client graph in three SQL passes:

1. **Audience runs** — `SELECT * FROM kol_extract_runs WHERE client_id = :cid AND extract_type = 'followers' AND cursor_completed = 1`. The `target_handle_normalized` of these is the client's audience-extraction targets (Doji/9dcc/Fabricant for SolStitch).
2. **Cohort runs** — `WHERE client_id = :cid AND extract_type = 'following' AND cursor_completed = 1`. The `target_handle_normalized` of these is the variant-2 cohort whose followings inform kingmaker counts.
3. **Edges** — `SELECT e.* FROM kol_follow_edges e JOIN kol_extract_runs r ON r.run_id = e.run_id WHERE r.client_id = :cid AND r.cursor_completed = 1`.

`kol_candidates` is shared across clients (the bank pool). The per-client *outreach plan* applies sector + filter passes from the client config. Two clients can have the same handle in their candidate pool, but the graphs (which audience pulled them, which kingmakers they overlap with) are scoped per-client via `kol_extract_runs.client_id`.

**Backfill plan for migration 039:** every existing SolStitch extract run gets `client_id='solstitch'`. `_external` becomes the default for any future run that doesn't supply a client_id (operator forgot to pass `--client`).

---

## Security: clientId path-traversal protection (audit finding #9 + Codex P2-3)

Both `/sable/clients/<clientId>.yaml` and `/sable/outreach/<clientId>/...` are filesystem reads driven by URL params. Outreach files are also accessed via `latest_*` symlinks. SableWeb hardens these with **four layers**:

* `clientId` regex validation — `^[a-z0-9_-]{1,32}$` (rejects `..`, slashes, dots, etc.).
* Discovered-id allowlist — `loadClientConfig` only opens `<clientId>.yaml` files where `clientId` matches a precomputed allowlist of YAML basenames discovered at startup. Unknown ids 404.
* `path.resolve()` base-path check — the resolved path must start with `/sable/clients/` or `/sable/outreach/`. Any escape returns 403.
* **`fs.realpath()` for symlinks (Codex P2-3)** — `path.resolve()` only resolves the link path itself, not the target. Outreach reads always go through `await fs.realpath(filePath)` and then re-verify the *target* is still under `/sable/outreach/`. Prevents a malicious symlink from pointing at `/etc/passwd` or another mount. Reject 403 if realpath escapes.
* All four checks live in `src/lib/client-config.ts:assertClientId(id)` and `src/lib/outreach-files.ts:assertSafePath(path)`. Every API route calls them first.

---

## Migration 038 — `kol_operator_relationships`

**Status: shipped.** Append-only relationship-tagging table. One row per status change.

**`is_private` semantics (audit finding #3):** the flag redacts only the `note` text, NOT the `status`. Status is always shared so two operators can't double-DM the same person. A private note ("had a weird vibe in the DM") is hidden from other operators while the shared status (`pass`) is still visible.

Read-time enforcement:

```sql
-- "Get current relationship state for a (handle, client_id) pair, with
--  notes redacted unless owned by the requesting operator."
SELECT id, status, created_at, operator_id,
       CASE WHEN is_private = 1 AND operator_id != :requesting_op
            THEN NULL
            ELSE note
       END AS note
FROM kol_operator_relationships
WHERE handle_normalized = :handle AND client_id = :cid
ORDER BY created_at DESC, id DESC
LIMIT 1
```

Same pattern for the history list — private notes appear as `note=null` for non-owners. Status timeline is fully shared.

```sql
-- 038_kol_operator_relationships.sql
CREATE TABLE IF NOT EXISTS kol_operator_relationships (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    handle_normalized TEXT NOT NULL,
    client_id TEXT NOT NULL,
    operator_id TEXT NOT NULL,
    status TEXT NOT NULL,
    note TEXT,
    is_private INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (status IN ('dm_sent', 'replied', 'replied_engaged',
                      'meeting', 'relationship', 'pass', 'blocked'))
);

CREATE INDEX IF NOT EXISTS idx_kor_handle_client
    ON kol_operator_relationships(handle_normalized, client_id);
CREATE INDEX IF NOT EXISTS idx_kor_operator
    ON kol_operator_relationships(operator_id, client_id);
CREATE INDEX IF NOT EXISTS idx_kor_created
    ON kol_operator_relationships(created_at);

UPDATE schema_version SET version = 38 WHERE version < 38;
```

6-surface parity (per `feedback_sableplatform_migration_sql` and the existing
037 pattern):

* `sable_platform/db/migrations/038_kol_operator_relationships.sql`
* `sable_platform/db/connection.py` — append `("038_kol_operator_relationships.sql", 38)`
* `sable_platform/db/schema.py` — `kol_operator_relationships` Table definition
* `sable_platform/alembic/versions/<rev>_kol_operator_relationships.py` — Alembic Postgres migration
* `sable_platform/db/migrate_pg.py` — `TABLE_LOAD_ORDER` adds `kol_operator_relationships` (no FK to other tables; `client_id` and `handle_normalized` are loose by design — clients/orgs may be `_external`, handles may not yet be in `kol_candidates`)
* `tests/db/test_migrations.py` — adds version-bump assertions to 38; new tests for `kol_operator_relationships`

---

## SableKOL changes

### Phase 2 generalization (~1 day, prereq)

* New `sable_kol/client_config.py` — typed `ClientConfig` dataclass loaded from `~/.sable/clients/<id>.yaml` or `/opt/sable/clients/<id>.yaml`. Schema:

  ```yaml
  client_id: solstitch
  display_name: SolStitch
  mode: stealth                      # stealth | public
  debut_date: 2026-05-28
  sector_focus: [fashion, culture, art, design, nfts, streetwear]
  themes: [...]                      # for vibe-fit scoring
  audiences:
    - { handle: doji_com, label: doji_audience, curator_weight: 2.0 }
    - { handle: 9dccxyz, label: 9dcc_audience, curator_weight: 2.0 }
    - { handle: thefabricant, label: fabricant_audience, curator_weight: 1.8 }
  manual_pins: [zigor, toomuchlag, nanixbt, auri_0x, loomdart]
  org_denylist_extras: []
  person_allowlist_extras: []
  celebrity_denylist_extras: []
  network_axes:
    x:
      label: "fashion ◂—————▸"
      keywords: [fashion, culture, art, design, streetwear, ...]
      saturation: 4
      archetype_boosts:
        artist: 0.5
        creator: 0.5
    y:
      label: "crypto-native ▴"
      keywords: [defi, nft, web3, onchain, ...]
      saturation: 4
      archetype_boosts:
        trader: 0.5
        anon: 0.5
  tier_thresholds:
    stealth:
      A: { max_followers: 15000, min_brokers: 4, min_vibe: 0.4 }
      B: { max_followers: 50000, min_vibe: 0.4 }
      C: { max_followers: 100000, min_vibe: 0.2, min_brokers: 2 }
    public:
      A: { min_followers: 100000 }
      B: { min_followers: 10000 }
      C: { min_followers: 1000 }
  ```

* Refactor `scripts/build_outreach_plan.py` and `scripts/build_network_graph.py` to take `--client <id>`, load config, eliminate hardcoded SolStitch constants. Default output dir from config.
* Refactor `scripts/ingest_audiences.py` to read audiences from config.

### Network-graph script — semantic axes (in-progress with this build)

* `scripts/build_network_graph.py` reads `network_axes` from client config
* New `axis_score(candidate, axis_config) -> float` function in `sable_kol/network_axes.py` shared across the network graph + outreach plan
* Embeds `axis_scores.x` / `axis_scores.y` per node in the JSON output. Labels read from per-client `_meta.network_axes.{x,y}.label`. NO SolStitch-specific field names like `fashion_score` — the field names stay generic so the same TS rendering code works for every client

### Outreach plan generator (in-progress)

* Adds `leads.json` and `leads.csv` outputs (flat array per Q11/B1)
* **Tags are NOT baked at write time** — see audit finding #2. The Python script writes the per-row data WITHOUT `operator_relationship`. SableWeb's `outreach-augment.ts` merges live tag state from Postgres at request time before streaming any download. This avoids tag staleness in deliverables
* Outputs to `/opt/sable/outreach/<client_id>/` in production, `~/Downloads/` for local dev (CLI flag)
* Writes BOTH the filtered (default) and `_full` (unfiltered) variants of every artifact (audit finding #7)
* Updates `latest_<mode>_<doc>.<ext>` symlinks after write

### Daily regenerate cron

* `sable-kol regenerate <client_id>` CLI subcommand — kicks off the full classify → score → outreach-plan flow for one client, writes outputs, logs cost. Wired into a Hetzner systemd timer (daily 03:00 UTC) once the build is live.

---

## SableWeb changes

### Filesystem mounts (Hetzner Docker)

```yaml
# docker-compose.yml additions
services:
  sable-web:
    volumes:
      - /opt/sable/clients:/sable/clients:ro
      - /opt/sable/outreach:/sable/outreach:ro
```

### New dependencies (audit finding #10)

* `@react-pdf/renderer` (~150KB on server, zero on client) — declarative PDF
* `yaml` (or `js-yaml`) — parses `/sable/clients/*.yaml`
* `d3-force` — already present

All PDF / download routes export `export const runtime = 'nodejs'` (NOT edge — `@react-pdf/renderer` requires Node APIs). The viewer page itself can stay on the default runtime.

### Library

* `src/lib/client-config.ts` — `loadClientConfig(clientId): Promise<ClientConfig>`. Reads YAML from mount, parses, caches 5 min. Type definition mirrors the Python schema. **Exports `assertClientId(id)` that runs the path-traversal checks from "Security" above** — every API route calls this first.
* `src/lib/kol-data.ts` — Postgres reads via existing `DbDriver`:
  * `loadKOLNetwork(clientId): {nodes, edges, kingmakers, stats}` — joins on `kol_extract_runs.client_id` (migration 039)
  * `loadOperatorRelationships(clientId, requestingOperatorId): {handle → {status, note, history, set_by, set_at}}` — note redacted to `null` for private rows the requesting operator doesn't own (audit finding #3)
* `src/lib/kol-tags.ts` — write side; calls `db-write.ts` for inserts:
  * `setOperatorTag({handle, clientId, operatorId, status, note, isPrivate})`
  * `getTagHistory({handle, clientId, requestingOperatorId})` — same redaction pattern
* `src/lib/outreach-files.ts` — reads files from `/sable/outreach/<client>/`. Resolves `latest_<mode>` symlinks; query params `?date=YYYY-MM-DD` select a specific dated file. Returns 404 if not found. Validates `clientId` first.
* `src/lib/outreach-augment.ts` — **(audit finding #2)** the tag-merge layer. Reads the base file (Python-generated, possibly hours stale), then queries the live tag table and overlays current status onto each candidate row. Returns a tag-augmented payload to the response stream.
* `src/lib/outreach-pdf.tsx` — `@react-pdf/renderer` `<Document>` component; takes the AUGMENTED report payload (not the raw file), returns PDF stream.

### API routes

```
GET  /api/ops/kol-network/[clientId]
       — node + edge JSON for the viewer
       — query params: ?roles=&tier=&sector=&min_pool=&min_followers=&size=
       — server-side trim to ≤5K nodes; ISR 5-min cache
       — joins kol_extract_runs.client_id = :cid (migration 039 required)

GET  /api/ops/kol-network/[clientId]/report.md     ?mode=stealth&date=YYYY-MM-DD&include_orgs=1&include_celebs=1
GET  /api/ops/kol-network/[clientId]/report.pdf    (same query params)
GET  /api/ops/kol-network/[clientId]/report.json   (same)
GET  /api/ops/kol-network/[clientId]/leads.json    (same)
GET  /api/ops/kol-network/[clientId]/leads.csv     (same)

       Resolution rules:
       1. mode defaults to client_config.mode (e.g. 'stealth' for SolStitch).
       2. date defaults to "latest" → resolves latest_<mode>_<doc>.<ext> symlink
          (where doc ∈ {report, leads}, derived from the URL path).
       3. include_orgs / include_celebs:
            - both 0 (default): serve the pre-filtered file (cache hit)
            - either or both 1: load the *_full variant from disk, then re-apply
              the filter(s) the operator did NOT request to disable, in TS via
              src/lib/kol-filters.ts. So include_orgs=1 alone returns orgs but
              still filters celebs; include_celebs=1 alone returns celebs but
              still filters orgs; both=1 returns the raw _full file.
       4. PDF is rendered on demand from JSON; not stored on disk.
       5. .md / .json / .csv: tag state merged at request time (audit finding #2).
       6. PDF: same merge, then rendered to PDF.
       7. Response: Content-Disposition: attachment; filename={client}_<doc>_{date}_{mode}.{ext}.

GET  /api/ops/kol-network/[clientId]/tags
       — { handle: { current_status, history[5], note, set_by, set_at } }
       — note redacted to null for private rows owned by other operators

POST /api/ops/kol-network/[clientId]/tags
       — { handle, status, note?, is_private? } → 201 with the new row
       — appends a new row to kol_operator_relationships
```

All routes go through `getSession()` + `isOpsRole()` + `assertClientId(clientId)`. **No web `/regenerate` route in v1** (audit finding #1) — refresh runs via Hetzner systemd timer (`sable-kol regenerate <client>` daily 03:00 UTC) and SSH-only emergency runs. Adding a web trigger requires a separate writable mount, advisory lock (`/opt/sable/outreach/<client>/.lock`), per-client budget guard, and admin-role check.

### Page + components

```
src/app/ops/kol-network/page.tsx                 — landing, redirects to default client
src/app/ops/kol-network/[clientId]/page.tsx      — main page (server component)
src/components/ops/KOLNetwork.tsx                — 'use client', d3-force semantic layout
src/components/ops/KOLControls.tsx               — left panel (sliders, filters, presets)
src/components/ops/KOLNodePanel.tsx              — right panel (click-target details + tagging)
src/components/ops/KOLDownloadButtons.tsx        — MD / PDF / JSON / CSV downloads
src/components/ops/KOLClientPicker.tsx           — header dropdown
src/components/exports/OutreachPDF.tsx           — @react-pdf <Document>
```

### `KOLNetwork.tsx` rendering loop

* Canvas-based (5K nodes hits SVG performance ceiling)
* d3-force simulation with `forceCollide(radius_per_node)` and `forceX/Y(target_per_node)` only
* Custom hit-test on click (loop nodes, distance check); cached spatial-index optional
* Pulse animation: `requestAnimationFrame` loop, top-30 by in_pool, amplitude ∝ in_pool / max_in_pool. Default OFF
* Tag badge: 8-10px colored circle drawn upper-right of each tagged node, redraws on tag change

### Permalink scheme (named params, not JSON-encoded)

```
/ops/kol-network/solstitch?size=less&roles=cand,cohort&tier=A&sector=fashion,art&color=in_pool
```

`KOLControls` reads/writes `useSearchParams()`; debounced 300ms to avoid URL spam.

### Mobile

`useMediaQuery` for `< 768px`:

* Network renders below the panel, not beside
* Default size bucket = `Least` (~150 nodes)
* Tag write disabled (tap-to-select shows panel; status change disabled)
* Same auth, same data

---

## Build order

1. **Migration 038** — author + tests + verify `pytest tests/db/` clean. **DONE.** ~1h.
2. **Migration 039** — `kol_extract_runs.client_id` column with backfill. Required for client-scoped graphs (audit finding #4). ~1h.
3. **SableKOL Phase 2 generalization** — `client_config.py`, refactor scripts to take `--client`, write configurable output dir, generic `axis_scores.{x,y}` field naming (audit finding #8). Land SolStitch YAML. Backfill `client_id='solstitch'` on existing runs via Phase-3 migration step. ~1 day.
4. **SableKOL outreach generator changes** — write to `/opt/sable/outreach/<client>/`, emit `latest_<mode>` symlinks, both filtered and `_full` variants (audit finding #7), `leads.json` + `leads.csv`. NO `operator_relationship` baked at write time — that gets merged at request time in SableWeb (audit finding #2). ~half day.
5. **SableKOL `regenerate` CLI subcommand** + Hetzner systemd timer (daily 03:00 UTC). NO web trigger in v1 (audit finding #1). ~2h.
6. **SableWeb scaffolding** — `loadClientConfig` + `assertClientId` (audit finding #9), page route, server component, auth gate. Add `@react-pdf/renderer` and `yaml` deps (audit finding #10). Empty UI but page-loads. ~2-3h.
7. **`KOLNetwork.tsx`** — d3-force semantic layout reading `axis_scores.{x,y}`, all controls. ~2-3 days.
8. **Download routes with tag merge** (audit finding #2) — MD/JSON/CSV file-serving + tag-state overlay + PDF render. ~half day.
9. **Tag side-panel + writes + node badge + color mode** — with note-redaction for private rows (audit finding #3). ~1 day.
10. **Multi-client picker** — once a second client has run. ~1h.
11. **Hetzner deploy** — Docker volumes (read-only mount to web container), systemd timer for daily regenerate, smoke-test on staging. ~half day.

Total: ~7-8 working days end-to-end, assuming SableKOL Phase 2 generalization (item 3) doesn't surface new structural issues.

---

## Acceptance criteria

* `/ops/kol-network/solstitch` loads in <2s on Hetzner production, shows the SolStitch network at "Less" density with semantic axes, all controls work
* Click a node → side panel opens; setting a status writes to `kol_operator_relationships`; refreshing the page shows the same status
* Two operators logged in simultaneously → operator A tags a KOL → operator B sees the tag on next refresh (5-min ISR cache acceptable; no websockets)
* `/api/.../report.pdf` returns a 1-3 page PDF with the canonical outreach plan shape, status column populated for tagged KOLs
* `/api/.../leads.json` returns <1MB flat-array JSON; `?include_orgs=1` includes orgs; default excludes them
* TIG / Multisynq / PSY YAML configs added → picker shows them; per-client tags isolated (no cross-client leak)
* Mobile (< 768px) renders read-only with sane defaults

---

## Out of scope (defer to Phase 2 / 3)

* Saved views (named filter states, shared/private)
* `/ops/kol-network/[clientId]/tagged` dedicated table view of all tagged KOLs
* Cross-client compare side-by-side
* Bulk tagging (multi-select + status batch)
* Annotations as a separate concept (collapsed into tags per Q12)
* Client-facing redacted view
* Websocket-based realtime tag updates
* Admin UI for editing per-client YAML (operator SSH-edits files for now)
* Spend-from-UI (any SocialData call kicked from web)
* 3D layout (Q1 locked at 2D)
* Edge bundling / ML-based cluster detection beyond the existing Jaccard pass

---

## Risks

* **SableKOL Phase 2 generalization drag.** If per-client YAML schema iteration takes longer than estimated, slippage cascades. Mitigate by checkpointing after item 2 — if the YAML schema isn't right, pause SableWeb work and fix it first.
* **5K nodes + canvas + d3-force performance.** Tested in the standalone HTML viewer; should port cleanly to React but Canvas-React drawing patterns differ. Have a fallback to render at "Less" density (400 nodes) if 5K stutters.
* **Postgres query shape for edges.** Naïve "all edges where both endpoints in {visible}" returns 134K rows for SolStitch. Server-side trim by score first, then bound the edge query. Index on `(follower_handle, followed_handle)` may need adding.
* **PDF render time on Hetzner CX21.** `@react-pdf/renderer` runs Node-only; 200-row plan should render in <2s. If slow, cache the PDF artifact to `/opt/sable/outreach/` alongside the MD.
* **Operator typo in YAML config.** No validation today. Add a `sable-kol client validate <id>` CLI step + run it on docker-compose up.
* **Tag table churn.** Append-only means many rows per active KOL. At 4K candidates × 7 statuses × 5 operators we top out at ~140K rows lifetime. Trivial. No GC needed.

---

## Audit response (2026-05-07 build-plan audit)

| # | Finding | Resolution |
|---|---------|-----------|
| 1 | Regenerate route contradicts "no spend from UI" + read-only mount | Removed from v1. Refresh via systemd timer + SSH only. Re-adding requires writable mount + lock + budget guard + admin role |
| 2 | Tags go stale in dat-baked downloads | Downloads merge live tag state from Postgres at request time. Python writes the base file; SableWeb overlays tags before streaming response (`outreach-augment.ts`) |
| 3 | `is_private` semantics under-specified | Clarified: `is_private` redacts only the `note` text, never the status. Status is always shared (operator coordination). Read query redacts notes via `CASE WHEN is_private = 1 AND operator_id != :requesting_op THEN NULL ELSE note END` |
| 4 | Client scoping data-model gap | Added migration 039 (`kol_extract_runs.client_id` column with SolStitch backfill). `loadKOLNetwork(clientId)` joins on this column. Section "Client → graph mapping" added |
| 5 | `MAX(created_at)` non-deterministic | Replaced everywhere with `ORDER BY created_at DESC, id DESC LIMIT 1` (id as tiebreaker). Migration test already used this pattern; doc text now matches |
| 6 | Filenames vs route conflict | Routes default to `latest_<mode>` symlinks; query params `?date=YYYY-MM-DD` select dated files. Python writes both dated files and updates symlinks |
| 7 | `include_orgs/include_celebs` impossible from filtered files | Python writes BOTH filtered and `_full` variants. Query params switch which variant the API serves |
| 8 | `(fashion_score, crypto_score)` SolStitch-specific | Replaced with generic `axis_scores.x` / `axis_scores.y`. Labels come from `_meta.network_axes.{x,y}.label` per-client config |
| 9 | Path traversal on filesystem reads | Added section "Security: clientId path-traversal protection". Three-layer check: regex validation, allowlist of discovered YAML basenames, `path.resolve()` base-path verification. `assertClientId(id)` called by every route |
| 10 | Missing dependencies / runtime | Listed `@react-pdf/renderer` and `yaml` as new deps. PDF/download routes specify `runtime = 'nodejs'` (not edge) |

### Codex round 2 (2026-05-07)

| # | Finding | Resolution |
|---|---------|-----------|
| P1-1 | `latest_<mode>.{ext}` ambiguous because both `report.json` and `leads.json` exist | Renamed pattern to `<client>_<doc>_<date>_<mode>.<ext>` and `latest_<mode>_<doc>.<ext>` (where `<doc> ∈ {report, leads}`). Symlinks and dated files both carry the discriminator |
| P1-2 | `include_orgs=1` alone would also include celebrities (and vice versa) when API switched to `_full` | API now loads `_full` and applies the filter the operator did NOT request to disable, in TS via `src/lib/kol-filters.ts`. Three states: both flags 0 (default, serve filtered file from cache), one flag 1 (load `_full`, re-apply the other filter), both flags 1 (serve raw `_full`) |
| P2-1 | Old `(fashion_score, crypto_score)` wording lingered at line 210 | Replaced with `axis_scores.x` / `axis_scores.y`; added explicit "no SolStitch-specific field names" instruction |
| P2-2 | Old "Python writes `operator_relationship`" wording lingered at line 215 | Replaced — the contract is now "tags NOT baked at write time; SableWeb merges at request time via `outreach-augment.ts`" |
| P2-3 | `path.resolve()` doesn't follow symlinks; `latest_*` could escape via malicious target | Added `fs.realpath()` step + re-verification of resolved target. New helper `src/lib/outreach-files.ts:assertSafePath()` |

### Implementation gaps Codex flagged (must close before live)

* **`SableKOL/sable_kol/socialdata_bulk.py:create_run()` doesn't accept `client_id` yet.** Migration 039 added the column with default `'_external'`, but new bulk-fetch runs would still default to that until the caller is updated. Fixing now.
* **`SableWeb/package.json` doesn't list `@react-pdf/renderer` or `yaml`.** Plan calls for them but the deps haven't been added. To be added during build-order item 6 (SableWeb scaffolding); explicitly noted in pre-merge gates below.

## Build-checklist (pre-merge gates)

Before each build-order item is considered done:

* [ ] All cross-repo tests pass (`SablePlatform/tests/`, `SableKOL/tests/`, eventually `SableWeb/tests/`)
* [ ] No `fashion_score` / `crypto_score` strings in any production code path. Audit-response sections in this doc are the only allowed mentions (they explain the rename)
* [ ] No "operator_relationship written by Python" patterns; tag merge is request-time only
* [ ] Every artifact filename includes a `<doc>` discriminator
* [ ] Every filesystem read goes through `assertClientId()` AND `assertSafePath()` (where the path was resolved)
* [ ] All symlink reads use `fs.realpath()` and re-verify base-path
* [ ] `socialdata_bulk.create_run()` accepts and persists `client_id`; default comes from the client config's id, NOT `_external`
* [ ] `package.json` includes `@react-pdf/renderer` and `yaml` before any download route lands
