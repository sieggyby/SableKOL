# CLAUDE.md — SableKOL

Conventions and load-bearing decisions for working in this repo. Read this file
for "how do I work here without breaking something." For deeper reads see
[`docs/ENRICHMENT.md`](docs/ENRICHMENT.md) (v2.5 SocialData-backed intel feature)
and [`docs/PERSONAS.md`](docs/PERSONAS.md) (operator priming). `docs/AUDIT_LOG.md`
records what's actually shipped + the why behind architectural pivots.

---

## What this is

Three coordinated surfaces:

1. **Bank ETL (CLI).** `sable-kol ingest / classify / crossref / find` builds the
   `kol_candidates` bank from X-list exports, classifies via Haiku, joins against
   `sable.db`'s entity tables, and ranks candidates for a target project.
2. **Any-project wizard (sidecar + SableWeb).** Operator submits a Twitter handle
   in the SableWeb ops UI; the SableKOL FastAPI sidecar (`preflight_service.py`)
   handles Grok preflight, comparable-project suggestion, reuse-check, and the
   job-step machine that surveys follow-graphs and produces deliverables.
3. **Per-candidate enrichment (v2.5).** Operator clicks a node in the network
   viewer, sidecar fetches the candidate's real tweets via SocialData, Grok
   interprets them against the operator's persona profile, returns intel
   (likes / dislikes / mutuals / commonality / commentary). Results cache in
   `kol_enrichment` per `(candidate_id, operator_email)`.

---

## Hard architectural rules

- **`sable.db` is owned by SablePlatform.** SableKOL writes to its tables
  (`kol_candidates`, `kol_extract_runs`, `kol_follow_edges`, `kol_enrichment`,
  etc., migrations 032-041) but does NOT define schema. New columns/tables go in
  SablePlatform with the dual SQL + Alembic migration pattern (see
  `SablePlatform/docs/EXTENDING.md`).

- **`sable-platform` is a hard dep.** Editable-installed in the venv. All
  connections come from `sable_platform.db.connection.get_db()` via
  `sable_kol.db.open_db()`. Don't roll your own SQLite path.

- **Slopper (`sable` package) is preferred when available, NOT required.**
  `sable_kol/socialdata_bulk.py` tries `from sable.shared.socialdata import
  socialdata_get` first; on `ImportError` falls through to `_httpx_socialdata_get`
  (same retry semantics, in-repo). The production sidecar's
  `Dockerfile.preflight` ships only the `[service]` extra, so it takes the
  fallback path — don't reintroduce a hard Slopper dep without expanding the
  Docker build context.

- **Every paid SocialData / Grok call writes a `cost_events` row** via
  `sable_kol.cost.record`. The v2.5 enrichment path logs two rows per call
  (`socialdata_enrich_profile` + `socialdata_enrich_tweets`); see
  `grok_api._default_cost_logger`. Path-(ii) external handles use
  `org_id='_external'` (sentinel auto-created).

- **v2.5 enrichment is grounded in real X material, NEVER Grok "live X search".**
  `grok-4-latest` does not have reliable real-time X access (verified live
  2026-05-10 — see `docs/AUDIT_LOG.md` for the verbatim admission). The current
  flow fetches profile + 20 tweets via SocialData, then hands the verbatim
  material to Grok to interpret. If you find yourself writing a prompt that
  says "use live X search" — stop, you're recreating the v2 failure mode.

- **SocialData's `/twitter/user/<screen_name>/tweets` endpoint 404s.** Only
  `/twitter/user/<numeric_id>/tweets` works. `fetch_live_signal` chains a
  profile fetch (gives `id_str`) before the tweets fetch. The public API
  for `fetch_recent_tweets(user_id, …)` takes the numeric ID explicitly.

- **`persona_priming.py` is canonical for operator personas.** Python-side
  `PersonaSlug` Literal + `PERSONAS` dict. SableWeb mirrors via the
  `sable-kol persona-manifest --json` CLI; the fixture at
  `SableWeb/tests/fixtures/persona_manifest.json` is regenerated in CI and
  locksteps the TS persona enum. Adding a slug requires updating both sides
  + emitting the fixture.

- **Evidence contract for Haiku rationales.** `match.py` enforces that Haiku
  output lists `used_evidence_keys`; unknown keys or fabrication-denylist
  phrases trigger a regenerate. Two failures → degrade to rule-prerank score
  with `"<excluded due to evidence violation>"` rationale.

- **Partial unique index on `kol_candidates(handle_normalized) WHERE
  is_unresolved=0`.** Live rows are unique per handle; unresolved duplicates
  are allowed and tracked in `kol_handle_resolution_conflicts`. Don't bypass
  `upsert_candidate()` — it's the entry point that handles collision routing.

- **No new SQL DDL in this repo.** New columns / tables / indexes go in
  SablePlatform migrations.

---

## Repo layout

```
sable_kol/
├── cli.py                # click entry — bank ETL, preflight, jobs, regenerate, enrich
├── db.py                 # bank + external-profile helpers, upsert_candidate
├── ingest.py             # ETL Stage 1 — parse X list export
├── classify.py           # ETL Stage 3 — Haiku archetype/sector tags
├── crossref.py           # ETL Stage 4 — sable.db join + Tier-2 fold-in
├── match.py              # hybrid scorer + Haiku rationales + evidence contract
├── grok_api.py           # xAI Grok client + enrich_candidate (v2.5)
├── grok_import.py        # historical Grok-paste import path
├── persona_priming.py    # canonical operator persona table (arf / sparta / alex / ben)
├── socialdata_live.py    # v2.5 SocialData fetcher (profile + recent tweets)
├── socialdata_bulk.py    # bulk follow-graph fetcher (with Slopper-or-httpx fallback)
├── preflight_schemas.py  # Pydantic types crossing the sidecar boundary
├── preflight_service.py  # FastAPI sidecar (/preflight, /enrich-candidate, /reuse-check)
├── handle_verifier.py    # SocialData ground-truth gate for Grok-suggested handles
├── follow_graph.py       # co-follow matrix + clustering for outreach plan
├── outreach_plan.py      # tiered plan generator + JSON/CSV serializers
├── regenerate.py         # systemd-timer entry (classify → score → outreach → network)
├── jobs.py               # wizard job runner + step machine (kol_create job_type)
├── wizard_orgs.py        # idempotent prospect-org upsert
└── cost.py               # cost_events logger

scripts/
├── build_outreach_plan.py
├── build_network_graph.py
├── phase6_extract_followings.py
├── backfill_handle_verification.py    # KO-5 — backfill SocialData verify on historical rows
└── ingest_audiences.py

Dockerfile.preflight         # sidecar image (in repo root)
deploy/SIDECAR.md            # operator runbook for the sidecar
deploy/jobs/                 # systemd timer for kol_create job worker
deploy/regenerate/           # systemd timer for daily client refresh
```

---

## Working conventions

- **Run tests with `.venv/bin/python -m pytest tests/ -q`.** Current suite is
  ~345 tests; all must pass before merging.
- **Don't bypass `sable_kol.db`** for `kol_candidates` writes — JSON encoding,
  partial-unique-index handling, and conflict routing live there.
- **All Anthropic + Grok clients are injectable.** Tests pass a fake; production
  builds the real client lazily. The same pattern applies to `enrich_candidate`'s
  `socialdata_fetcher` and `cost_logger` kwargs.
- **`docs/AUDIT_LOG.md` records shipped work + design rationale.** Add an entry
  when landing something non-trivial; don't rely on commit messages alone.

---

## Common tasks

| Goal | How |
|------|-----|
| Run all tests | `.venv/bin/python -m pytest tests/ -q` |
| Add a candidate row | `from sable_kol.db import upsert_candidate` (NEVER raw INSERT) |
| Log a paid call | `from sable_kol.cost import record` |
| Pull live tweets for a handle | `from sable_kol.socialdata_live import fetch_live_signal` |
| Regenerate a client | `sable-kol regenerate <client_id>` (systemd-timer fires daily) |
| Backfill handle verification | `python scripts/backfill_handle_verification.py --filter risky` |
| Emit persona manifest (for SableWeb fixture) | `sable-kol persona-manifest --json` |
| Skip eval tests in dev | `SABLEKOL_SKIP_EVAL=1 pytest` |
