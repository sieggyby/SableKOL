# SableKOL — Plan (v3, post-audit-2)

**Status:** Pre-implementation. Design locked via /grill-me on 2026-05-04. Audit pass 1 corrected same day → v2. Audit pass 2 corrected same day → v3.
**Framing:** **Phase 0: bank-backed KOL matcher.** Precursor to the full SableGraph MVP described in `~/Downloads/SableGraph_Product_Spec (1).md`. SableKOL Phase 0 ships a free, bank-only matcher; the spec's Phase 1 (graph store, follower-overlap discovery via SocialData, SableWeb page, action creation) is **not** in Phase 0 scope. Calling this "the SableGraph MVP" overstates it — call it the precursor.

**Audit log:**
- v1 rejected for (1) migration number conflict, (2) inconsistent migration scope, (3) free-path contradiction on external handles, (4) wrong SocialData endpoint, (5) overclaimed lineage, (6) wrong watchlist storage layer.
- v2 rejected for (1) wrong Alembic path (`db/alembic` vs `alembic`), (2) wrong Slopper dep name (`sable-slopper` vs `sable`), (3) `is_unresolved` referenced but not declared and conflicting with UNIQUE handle, (4) "2+ org appearances" not computable from `entity_handles` (UniqueConstraint blocks it) and `tracked` not a real entity status. Plus high-risk fixes on TTL, cost_events, evidence-contract enforcement, and bank-coverage-vs-ranker eval split.
- v3 fixes all of v2's blockers and applies all four high-risk repairs.

---

## Mission

Take a project (Sable client/prospect or external Twitter handle) and return a ranked list of CT KOLs worth engaging, with a one-line rationale per KOL and a flag where Sable already has a connection. Build a free candidate bank first; route any paid Twitter access through Slopper's existing wrapper.

---

## KOL archetypes — what we're matching

**In scope from Phase 0:**
- **(b) Organic thought leaders** — researchers, anons, devs, founders with credibility and following.
- **(c) Community connectors / bridge nodes** — cross-sector reach, podcasts/spaces hosts, well-followed by other KOLs.

**In scope but parallel layer (manual coworker entry; not gating Phase 0 matcher):**
- **(a) Paid-promo KOLs.** Stored on bank rows as supplemental metadata; surfaced in a separate output column once data exists.

---

## Project input shapes

### Path (i) — `--org <org_id>`
Existing Sable client/prospect in `sable.db.orgs`. Profile assembled from:
- `orgs.config_json` → sector, stage
- `entity_tags` → community shape
- **Voice docs** at `~/.sable/profiles/@<handle>/{tone,interests,context,notes}.md` concat'd as `project_voice_blob`. Falls back to sector/stage/tags-only if voice docs are absent (no error).

### Path (ii) — `--handle <h> --sector <s>` (profile-only by default)
External project not in sable.db.

**Phase 0 default behavior (FREE PATH):** No SocialData call. The operator supplies `--sector <s>` and optionally `--themes "yield,rwa,solana"` flags. The matcher uses *only* operator-supplied metadata + the handle as a label. Cached in `project_profiles_external`.

**`--paid-enrich` flag (OPT-IN, NOT DEFAULT):** Single SocialData call to `GET /twitter/user/{handle}` for bio + follower count + verified status. Cached in `project_profiles_external` with **TTL = 7 days** per Sable's SocialData guidance (`docs/SOCIALDATA_BEST_PRACTICES.md`). `last_enriched_at` is checked on every `--paid-enrich` invocation; stale entries auto-refetch. `--refresh-paid` flag forces refresh regardless of TTL. **No tweet timeline call in Phase 0** — that's Phase 2 territory. One call per refresh, ~$0.002, logged to `cost_events` (see Cost accounting below).

This resolves the audit's concern (#3): the free path is genuinely free; paid enrichment is opt-in with an explicit flag and an explicit single-call cost cap.

**Deferred to Phase 2+:** free-text descriptions, whitepapers, sector-only exploration mode, tweet-timeline-driven external profiles.

---

## Free Phase 0 KOL bank

### Sources

| Tier | Source | Cost | Cadence |
|------|--------|------|---------|
| 1 | Cahit's "best of cryptotwit" X list, seeded once via manual browser HTML export | Free, ~5 min labor | Manual; programmatic refresh deferred to Phase 2 |
| 2 | sable.db `entities` with `entity_tags.tag='voice' AND is_current=1`, OR `entities.status='confirmed'`. (Cross-org "2+ appearances" is NOT computable from `entity_handles` — its `UniqueConstraint(platform, handle)` means each handle maps to exactly one entity. Real cross-sector signal needs Phase 2 mention-activity from `meta.db`.) | Free, internal | Auto, on every `crossref` run |
| 3 | Manual / coworker import (CSV) — also the entry point for paid-promo (a) tagging | Free, labor | As needed |

**Explicitly deferred:** Nitter, Twitter API free tier, LunarCrush/DeBank rankings, follower-of-followers mining.

### Schema — migration 032

Single migration `032_kol_bank` adds **two tables** (resolves audit #2):

#### `kol_candidates`

```
candidate_id           INTEGER PRIMARY KEY AUTOINCREMENT  (surrogate; lets multiple rows share handle when unresolved)
twitter_id             TEXT, nullable, indexed (backfilled by paid enrichment)
handle_normalized      TEXT, NOT NULL, lowercased, stripped
is_unresolved          INTEGER NOT NULL DEFAULT 0  (1 = collision/recycled-handle case awaiting manual review)
handle_history_json    TEXT (JSON array of past handles seen for this twitter_id, with timestamps)
display_name           TEXT
bio_snapshot           TEXT
followers_snapshot     INTEGER
discovery_sources_json TEXT (JSON array)
first_seen_at          TEXT (ISO)
last_seen_at           TEXT (ISO)
archetype_tags_json    TEXT (JSON array)
sector_tags_json       TEXT (JSON array)
sable_relationship_json TEXT (JSON; strict schema, see below)
enrichment_tier        TEXT (none | basic | deep)
last_enriched_at       TEXT (ISO; null = bank-only)
status                 TEXT (active | low_signal | suspended | dormant | dropped)
manual_notes           TEXT

-- Partial unique index: at most one LIVE row per normalized handle.
-- Unresolved rows do not collide with live rows or with each other.
-- SQLite supports partial indexes (3.8.0+); Postgres uses identical syntax.
CREATE UNIQUE INDEX idx_kol_candidates_handle_live
  ON kol_candidates(handle_normalized) WHERE is_unresolved = 0;
```

**Companion table `kol_handle_resolution_conflicts`** (also in migration 032) records collision events for audit/triage:

```
conflict_id            INTEGER PRIMARY KEY AUTOINCREMENT
incoming_candidate_id  INTEGER NOT NULL  (FK kol_candidates.candidate_id, the row that got is_unresolved=1)
existing_candidate_id  INTEGER NOT NULL  (FK kol_candidates.candidate_id, the live row that already held this handle)
resolved_twitter_id    TEXT              (the twitter_id from paid enrichment that exposed the conflict)
detected_at            TEXT (ISO)
resolution_state       TEXT (open | merged | superseded | discarded)
resolved_at            TEXT (ISO, nullable)
notes                  TEXT
```

**`sable_relationship_json` strict schema** (resolves audit's high-risk concern #2):
```json
{
  "communities": [{"org_id": "tig", "last_seen": "2026-04-20", "tags": ["voice", "tracked"]}],
  "operators":   [{"name": "alice", "relation": "follows" | "knows" | "warm_intro_possible"}]
}
```
Empty arrays = no relationship. Multiple of each are first-class.

**`handle_history_json`** (resolves audit's high-risk concern #1): unique-handle constraint is partial (`WHERE is_unresolved = 0`), so collisions create a new row with `is_unresolved=1` rather than failing the upsert. When `twitter_id` is later resolved by paid enrichment, the resolution flow:
1. If the resolved `twitter_id` already exists on a different live row, the existing live row keeps `is_unresolved=0`, the new one gets `is_unresolved=1`, and a `kol_handle_resolution_conflicts` row is recorded with `resolution_state='open'`.
2. If the resolved `twitter_id` matches the live row's expected identity, the old `handle_normalized` (if changed) is appended to `handle_history_json`.
3. Operator runs `sable-kol bank resolve --conflict <id>` to merge/supersede/discard via the conflicts table; manual-only in Phase 0.

#### `project_profiles_external`

Cache of path (ii) lite profiles so repeat queries are free.

```
handle_normalized      TEXT, primary key
twitter_id             TEXT, nullable
sector_tags_json       TEXT
themes_json            TEXT
profile_blob           TEXT (operator-supplied + optional bio from --paid-enrich)
enrichment_source      TEXT (manual_only | paid_basic)
last_enriched_at       TEXT (ISO; drives 7-day TTL for paid_basic)
created_at             TEXT
last_used_at           TEXT
```

**Caching policy:** `paid_basic` rows are valid for 7 days from `last_enriched_at`. Beyond that, `--paid-enrich` re-fetches automatically (writing a new `cost_events` row each time). `--refresh-paid` forces a fetch regardless of age. `manual_only` rows have no TTL — operator-supplied data only changes when the operator updates it.

#### Migration deliverables (dual-migration mandate per `EXTENDING.md`)

1. `sable_platform/db/migrations/032_kol_bank.sql` — SQLite, idempotent, append-only, self-versioning `UPDATE schema_version`.
2. `sable_platform/alembic/versions/<rev>_kol_bank.py` — Postgres revision generated via `alembic revision --autogenerate` after `schema.py` is updated. (Alembic root is `sable_platform/alembic/` per `alembic.ini` `script_location` — NOT `sable_platform/db/alembic/`.)
3. `sable_platform/db/schema.py` — add `Table("kol_candidates", ...)`, `Table("project_profiles_external", ...)`, and `Table("kol_handle_resolution_conflicts", ...)` definitions. Use `Text` for all JSON-bearing columns (consistent with existing platform convention — no GIN indexes in v1). Partial unique index on `kol_candidates(handle_normalized) WHERE is_unresolved = 0` declared via SQLAlchemy `Index(..., sqlite_where=text("is_unresolved = 0"), postgresql_where=text("is_unresolved = 0"))`.
4. Migration tests under `sable_platform/tests/db/test_migration_032.py` — assert all three tables exist, partial unique index works (insert two unresolved rows with same handle → both succeed; insert two live rows with same handle → second fails), `UPDATE schema_version` ran. Mirror existing 031 test pattern.
5. Update `connection.py` registry: `("032_kol_bank.sql", 32)`.

**Reverted decisions from v1 of plan:** No GIN index suggestion. JSON columns are Text. Pre-rank queries fetch rows and filter in Python; if Phase 2 query patterns demand server-side JSON filtering, that's a Phase 2 change with its own migration.

### ETL pipeline

1. **Scrape** — manual HTML export of Cahit's list → `sable-kol ingest --list-export <file>`.
2. **Resolve `twitter_id`** — deferred. Backfilled when paid enrichment first touches a row.
3. **Classify** archetype + sector tags via Haiku → JSON. Re-classify only on bio change. (`sable-kol classify`)
4. **Cross-reference** sable.db `entities` — handle joins populate `sable_relationship_json`. (`sable-kol crossref`)
5. **Dedupe + upsert** keyed on `handle_normalized`. Multi-source rows accumulate `discovery_sources_json`.
6. **Soft filter** — drop classifier-flagged `drop`, drop `<2K followers`, retain everything else.

### Refresh

- Phase 0: manual one-shot. Re-run when desired.
- Phase 2: programmatic Cahit refresh via Slopper's `socialdata_get_async` (if SocialData supports list-members; otherwise stays manual).

---

## Matcher — hybrid scoring + Haiku rationales + evidence contract

### Pipeline

1. **Project profile build.** Path (i) reads sable.db + voice docs. Path (ii) reads `project_profiles_external` (creating it from operator-supplied flags or `--paid-enrich`).
2. **Rule-based pre-rank.** SQL fetches the candidate set, then Python computes weighted score over JSON-decoded fields:
   - `sector_overlap` — set intersection of `sector_tags` ∩ project sector + adjacent sectors
   - `archetype_match` — project profile preference over thought_leaders/connectors
   - `bio_keyword_sim` — `project_voice_blob` keywords ∩ `bio_snapshot` (only if bio_snapshot present; else weight = 0 with note in evidence)
   - `sable_relationship` bonus — derived from `sable_relationship_json` (in_client_community high, followed_by_operator high)
   - `cross_org_centrality` proxy — count of `discovery_sources_json` org_ids
   Returns top **K=30**.
3. **Haiku rationale + re-rank** under an evidence contract.

### Evidence contract (resolves audit's high-risk concern #4)

The Haiku prompt receives a strict `candidate_evidence` dict per candidate — only fields actually present in the bank row. The system prompt explicitly instructs:

> "You may only cite signals from the candidate_evidence object. Return JSON with fields: `score` (0-100), `rationale` (one sentence), `used_evidence_keys` (array of dotted-path keys from candidate_evidence that your rationale references). Do NOT invent attributes (e.g., reply-rate, engagement, recent topics) that are not present. If you have no evidence basis, return `rationale: 'No strong signal beyond sector/archetype match.'` and `used_evidence_keys: []`."

**Hard validation, not just warning.** The matcher validates:
1. Every key in `used_evidence_keys` resolves against the `candidate_evidence` dict (e.g., `"sable_relationship.communities"` must exist).
2. The rationale text is checked against a denylist of fabrication-prone words ("reply-rate", "engagement-rate", "recent tweets", "active poster", "frequent", etc.) — any hit forces a regeneration.

**On violation:**
- **Retry once** with the prompt augmented by `"Your previous response cited X which is not in candidate_evidence. Regenerate using only the keys listed in candidate_evidence."`
- **On second failure:** the candidate ships with `score = rule_prerank_score`, `rationale = "<excluded due to evidence violation>"`, and the violation is recorded in `query_metadata.evidence_violations` (count) and `query_metadata.warnings` (per-candidate detail).

This is hard rejection, not soft warning. Phase 0 has no reply-rate, engagement, or recent-topic signals; rationales that fabricate them are bugs, not stylistic quirks.

### Output JSON

```json
{
  "project": { "org_id": "...", "sector": "...", "stage": "...", "themes": [...], "source": "org" | "external_manual" | "external_paid_basic" },
  "results": [
    {
      "twitter_id": null,
      "handle": "...",
      "display_name": "...",
      "followers": 12345,
      "score": 87,
      "signal_breakdown": {
        "sector_overlap": 0.9,
        "archetype_match": 0.8,
        "bio_sim": 0.6,
        "sable_relationship": 1.0,
        "centrality": 0.5
      },
      "candidate_evidence": {
        "archetype_tags": ["thought_leader", "researcher"],
        "sector_tags": ["defi", "sol"],
        "bio_snapshot": "DeFi research, ex-Jump…",
        "sable_relationship": { "communities": [{"org_id": "tig"}], "operators": [{"name":"alice","relation":"follows"}] },
        "discovery_sources": ["cahit_list", "tig"]
      },
      "rationale": "DeFi/Solana sector match; appears in TIG community; followed by Alice. No recent-tweet signals available in Phase 0.",
      "enrichment_tier": "none"
    }
  ],
  "query_metadata": {
    "cost_usd": 0.12,
    "candidates_considered": 1500,
    "k_evaluated": 30,
    "evidence_violations": 0,
    "warnings": [],
    "generated_at": "..."
  }
}
```

### Cost envelopes per query

| Path | SQL pre-rank | LLM rationales | SocialData | Total |
|------|-------------|----------------|------------|-------|
| Phase 0 free hot path (paths i + ii default) | ~0 | ~$0.05–0.15 (Haiku, K=30) | $0 | **<$0.20** |
| Phase 0 path (ii) `--paid-enrich` | ~0 | ~$0.05–0.15 | $0.002 (one profile call, TTL 7d) | **<$0.20** |
| Phase 2 enriched (later) | ~0 | ~$0.10–0.30 | $1–3 (top-K timelines via Slopper) | **~$1–3** (matches spec) |

### Cost accounting (resolves audit high-risk #2)

Slopper's `socialdata_get_async` is an error/rate-limit/retry wrapper — it does **not** write to `cost_events`. SableKOL is responsible for logging every paid call.

After every successful SocialData call, SableKOL writes a `cost_events` row via SablePlatform's existing cost API:

```python
# pseudocode
record_cost_event(
    org_id=<resolved-or-"_external">,
    tool="sablekol",
    call_type="socialdata_user_profile",  # or "anthropic_haiku_rationale"
    cost_usd=0.002,
    metadata={"handle": "...", "endpoint": "/twitter/user/{handle}"},
)
```

LLM costs (Haiku rationales, the path-(ii) lite-profile classifier) likewise log `cost_events` rows with `call_type="anthropic_haiku_..."`. This makes SableKOL's spend visible to the existing platform cost forecast (`sable serve`'s cost endpoints) without needing a separate accounting layer.

**Org_id resolution rule:** path (i) uses the `--org` value. Path (ii) uses the literal `"_external"` org_id (a sentinel; not a real org). Path (ii) `--paid-enrich` calls thus do not bill against any client's `max_ai_usd_per_org_per_week` budget — they're SableKOL R&D spend. If/when an external project becomes a Sable org, retroactive reattribution is out of scope.

---

## Evaluation loop (resolves audit's high-risk concern #5)

A small gold set lands alongside Phase 0:

- **5 projects** spanning sectors (e.g. tig, multisynq, a DeFi prospect, a gaming prospect, a Solana infra prospect)
- **20 known-good KOLs per project** (curated by Sable operators — "if these aren't in the top 50 returned, the matcher is broken")
- Stored at `~/Projects/SableKOL/eval/gold_set.yaml`

**`sable-kol eval` reports two distinct metrics** (resolves audit's bank-coverage concern):

1. **Bank coverage** — of all gold-set KOLs across all projects, what fraction exist as rows in `kol_candidates` at all? This measures the *ingest pipeline's* completeness; a ranking algorithm cannot rank what it has not been shown. Reported per-source (Tier 1 Cahit / Tier 2 sable.db / Tier 3 manual). Failures here are an ETL bug, not a ranker bug.
2. **Ranker recall (top-N)** — of gold-set KOLs that *are* present in the bank for a given project, what fraction appear in the top-N matcher results? Computed only on the in-bank subset. N = 20 and N = 50 reported separately.

**Regression tests are split:**
- `tests/integration/test_eval_bank_coverage.py` — asserts coverage ≥ tunable threshold (start at 0.5 — half the gold set should be in the bank). Failure indicates "add more sources" or "fix ingest."
- `tests/integration/test_eval_ranker_recall.py` — asserts top-50 recall over the in-bank subset ≥ tunable threshold (start at 0.6; raise as scoring matures). Failure indicates "fix the scoring function."

This split prevents ranker recall from being silently penalized by ingest gaps. Both tests ship in Phase 0; both can be skipped via env var (`SABLEKOL_SKIP_EVAL=1`) for fast local iteration but must run in CI.

---

## Architecture

### Repo

`~/Projects/SableKOL/` — own `.venv` per Sable convention. Python CLI.

### Cross-repo dependency strategy (resolves audit blocker #4 second half)

`pyproject.toml`:

```toml
[project]
dependencies = [
  "sable-platform @ file://localhost/Users/sieggy/Projects/SablePlatform",
  "anthropic", "click", "httpx", "pydantic", "pyyaml",
]

[project.optional-dependencies]
paid-enrich = [
  # Sable_Slopper's pyproject.toml declares name = "sable"; the dep name must match.
  "sable @ file:///Users/sieggy/Projects/Sable_Slopper",
]
```

- **`sable-platform` is a hard dep** (always required) — for `get_db()`, `kol_candidates` schema, migration utilities. Editable install: `pip install -e ../SablePlatform` into SableKOL's venv.
- **Slopper (`sable` package) is an optional dep** behind the `paid-enrich` extra. Without it installed, `--paid-enrich` raises a friendly error: `"--paid-enrich requires Slopper. Install with: pip install -e '.[paid-enrich]'"`. Phase 0 default flow doesn't need it. Note: Slopper's package name in its `pyproject.toml` is literally `sable` (not `sable-slopper`); SableKOL imports as `from sable.shared.socialdata import socialdata_get_async`.
- **No raw SocialData client in SableKOL.** All paid Twitter calls route through `from sable.shared.socialdata import socialdata_get_async`. Single rate-limit, single cost-tracking surface.

**Phase 2 cleanup:** move `socialdata_get_async` from Slopper into SablePlatform as a shared module, so both Slopper and SableKOL import from the platform. Avoids the editable-install dance. Out of Phase 0 scope.

### SableKOLAdapter in SablePlatform

Subprocess-adapter pattern per `SubprocessAdapterMixin._python_for(repo)` — must use `~/Projects/SableKOL/.venv/bin/python`, never `sys.executable`. Stub for Phase 0 (not wired into cron); wired into orchestration in Phase 2.

### Slopper integration — corrected (resolves audit blocker #6)

Slopper's watchlist is YAML at `~/.sable/pulse/watchlist.yaml`, accessed via `sable.pulse.meta.watchlist` (`add_handle`, `remove_handle`, etc.). Phase 2 integration calls those YAML helpers — **not** direct `meta.db` writes. `meta.db` is the scan-output store, not the membership store.

Phase 2 plan:
- After matcher run, top-K bank rows are passed to Slopper's `add_handle` to populate the watchlist YAML.
- Slopper's existing weekly meta-scan picks them up on the next cycle.
- Matcher reads pre-scanned signals from `meta.db` (topics, cadence, format) on subsequent runs, adding `recent_topical_alignment` and `engagement_estimate` to the scoring rule.
- This still happens via Slopper imports (under the `[paid-enrich]` extra), not direct DB access from SableKOL.

### Voice docs

Path (i) reads four markdown files at `~/.sable/profiles/@<handle>/{tone,interests,context,notes}.md`, concats, raw-injects into the Haiku prompt as `project_voice_blob`. No parsing — per Slopper convention.

---

## CLI surface

```
sable-kol ingest --list-export <html-or-json>     # ETL stage 1
sable-kol classify [--limit N]                    # ETL stage 3
sable-kol crossref                                # ETL stage 4
sable-kol find --org <org_id> [--limit 20]        # path (i)
sable-kol find --handle <h> --sector <s> \
                [--themes "..."] [--paid-enrich]  # path (ii); --paid-enrich opt-in
sable-kol bank stats                              # diagnostic
sable-kol bank dump --handle <h>                  # diagnostic
sable-kol bank resolve --conflict <id>            # manual conflict resolution
sable-kol eval                                    # gold-set bank-coverage + ranker-recall report
```

---

## Phasing

### Phase 0 (~2 weeks) — ship-first

- New repo at `~/Projects/SableKOL/`, own venv.
- `sable-platform` dep installed; Slopper (`sable` package) optional under `[paid-enrich]`.
- **Migration 032** in SablePlatform: SQL file at `sable_platform/db/migrations/032_kol_bank.sql` + Alembic revision at `sable_platform/alembic/versions/<rev>_kol_bank.py` + `schema.py` updates + migration test at `sable_platform/tests/db/test_migration_032.py`. Three tables (`kol_candidates`, `project_profiles_external`, `kol_handle_resolution_conflicts`) with partial unique index for live-handle uniqueness.
- ETL stages 1–6 (manual scrape input). Tier 2 source uses `entity_tags.tag='voice' AND is_current=1` OR `entities.status='confirmed'` (cross-org "2+" deferred to Phase 2).
- Matcher: hybrid scoring with **enforced** evidence contract (hard regenerate-on-violation, then degrade), Haiku rationales, K=30, both paths (i) and (ii).
- Path (ii) is profile-only by default; `--paid-enrich` opt-in for one SocialData call with 7-day TTL; `--refresh-paid` forces refresh.
- Voice doc integration in path (i) prompt.
- **All paid calls (SocialData + Anthropic) write `cost_events` rows** with `tool='sablekol'`. Path (ii) external uses `org_id='_external'` sentinel.
- CLI only. JSON + terminal output. **No SableWeb.**
- `SableKOLAdapter` stub in SablePlatform (not wired into cron).
- **Gold-set eval split into bank-coverage and ranker-recall**, with two regression tests (`test_eval_bank_coverage.py`, `test_eval_ranker_recall.py`).

### Phase 2

- Slopper watchlist sync (top-K → `~/.sable/pulse/watchlist.yaml` via Slopper's `add_handle`).
- Matcher reads `meta.db` scan outputs; adds `recent_topical_alignment` and `engagement_estimate` signals.
- Programmatic Cahit refresh (if SocialData list endpoint supports it).
- SableWeb KOL page rendering the same JSON.
- Manual relationship-tagging UI (operators mark entities they personally know).
- Adapter wired into SablePlatform's weekly cron.
- Move `socialdata_get_async` from Slopper into SablePlatform shared module.

### Phase 3+

- Cross-community talent sourcing.
- Outreach action wiring + feedback loop.
- NL query interface.
- Paid-promo (a) layer with `kol_promo_meta` table.
- Operator follow-list import (after privacy decision per spec Open Q3).
- Real cross-org entity resolution (alias matching, confidence, manual confirm UI).

### Explicitly NOT in Phase 0 (vs. SableGraph spec)

This is what makes Phase 0 a precursor, not the spec's MVP:
- No graph store, no follower-overlap discovery, no following-of-following.
- No tweet-timeline-driven scoring.
- No SocialData hot-path calls (one optional profile call only, opt-in).
- No SableWeb integration.
- No outreach action creation.
- No continuous graph updates / workflow orchestration.
- No cross-org entity resolution as a project.
- No network-centrality computation.

When all of these are in, *that* is the SableGraph MVP.

---

## Open items (resolve during Phase 0)

- **Manual list-export format.** Validate by exporting Cahit's list once and inspecting before writing the parser.
- **`twitter_id` resolution for path (ii) `--paid-enrich`.** SocialData `GET /twitter/user/{handle}` returns it. Cache in `project_profiles_external.twitter_id`.
- **Voice doc absence.** Path (i) falls back to sector/stage/tags-only profile. Don't error.
- **Migration 032 scope.** Two tables, one migration, dual SQL + Alembic. Add no indexes beyond what's mandatory for the unique constraints + foreign-keyless cross-org lookup.
- **Recycled-handle collisions.** When paid enrichment resolves `twitter_id` and finds a different bank row already mapped to it, the duplicate gets `is_unresolved=1` (no constraint violation thanks to the partial unique index) and a `kol_handle_resolution_conflicts` row is written. `sable-kol bank stats` surfaces open conflicts; `sable-kol bank resolve --conflict <id>` lets the operator merge/supersede/discard. Phase 0 is manual-only; auto-resolution heuristics are Phase 2+.
- **Gold-set curation.** Need 5 projects × 20 KOLs from operator input before Phase 0 ships. Block the `eval` regression test on this.

---

## What's NOT in Phase 0

- No GUI / SableWeb work.
- No new SocialData-paying code paths in the default hot path. `--paid-enrich` is the single exception: one cached profile call per external handle.
- No twscrape, Nitter, or non-Slopper Twitter scraping.
- No paid-promo rate-card matching.
- No follower/following graph traversal.
- No real-time / continuous bank refresh.
- No operator follow-list import until privacy decision.
- No direct `meta.db` reads/writes from SableKOL — Slopper's API only.
- No GIN indexes or Postgres-specific JSON ops in migration 032.
