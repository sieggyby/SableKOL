# CLAUDE.md — SableKOL

Conventions and load-bearing decisions for working in this repo. Read `PLAN.md`
for the design rationale (audit-corrected v3); read this file for "how do I
work here without breaking something."

---

## What this is

**Phase 0: bank-backed KOL matcher.** Precursor to the SableGraph MVP. Takes a
project (Sable client/prospect or external Twitter handle) and returns a
ranked list of CT KOLs with one-line rationales. The bank is built free from
Cahit's "best of cryptotwit" X list plus sable.db cross-org voices; paid
SocialData enrichment is opt-in (`--paid-enrich`).

---

## Hard architectural rules

- **`sable.db` is owned by SablePlatform.** SableKOL writes to its three
  migration-032 tables (`kol_candidates`, `project_profiles_external`,
  `kol_handle_resolution_conflicts`) but does NOT define schema. New schema
  changes go in SablePlatform with the dual SQL + Alembic migration pattern
  (see `SablePlatform/docs/EXTENDING.md`).

- **`sable-platform` is a hard dep.** Editable-installed in this repo's venv.
  All connections come from `sable_platform.db.connection.get_db()` via
  `sable_kol.db.open_db()`. Don't roll your own SQLite path.

- **Slopper (`sable` package) is an optional dep** behind the `[paid-enrich]`
  extra. Without it, `--paid-enrich` raises a friendly error. With it,
  SableKOL imports `from sable.shared.socialdata import socialdata_get` for
  the single profile-lookup call — never bypass Slopper's wrapper to avoid
  duplicating rate-limit / 402 / 429 handling.

- **Every paid call writes a `cost_events` row** via `sable_kol.cost.record`.
  Slopper's wrapper does NOT log cost. SableKOL is responsible. Path-(ii)
  external handles use `org_id='_external'` (sentinel org auto-created on
  first use).

- **Evidence contract is hard-enforced.** Haiku rationales must list
  `used_evidence_keys`; unknown keys or fabrication-denylist phrases trigger
  a regenerate. Two failures → degrade to rule-prerank score with
  `"<excluded due to evidence violation>"` rationale. See `match.py`.

- **No tweet-timeline calls in Phase 0.** `--paid-enrich` is bio + follower
  count + verified status only. Anything else is Phase 2 territory.

- **Partial unique index on `kol_candidates(handle_normalized) WHERE is_unresolved=0`.**
  Live rows are unique; unresolved duplicates are allowed and tracked in
  `kol_handle_resolution_conflicts`. Don't bypass `upsert_candidate()` —
  it's the one entry point that handles collision routing.

---

## Repo layout

```
SableKOL/
├── PLAN.md                   # canonical design (v3, audit-corrected)
├── CLAUDE.md                 # this file
├── README.md                 # user-facing quickstart
├── pyproject.toml
├── .venv/                    # own venv (Sable convention)
├── sable_kol/
│   ├── cli.py                # click entry point — `sable-kol ...`
│   ├── db.py                 # bank + external-profile + conflict helpers
│   ├── ingest.py             # ETL Stage 1 — parse X list export
│   ├── classify.py           # ETL Stage 3 — Haiku archetype/sector tags
│   ├── crossref.py           # ETL Stage 4 — sable.db join + Tier-2 fold-in
│   ├── diagnostics.py        # bank stats / dump / resolve
│   ├── profile.py            # project-profile builders (paths i + ii)
│   ├── match.py              # hybrid scorer + Haiku rationales + evidence contract
│   ├── eval.py               # gold-set bank-coverage + ranker-recall
│   └── cost.py               # cost_events logger
├── eval/
│   └── gold_set.yaml         # 5 projects × 20 KOLs, operator-curated
└── tests/
    ├── conftest.py           # CompatConnection-backed db_conn fixture
    ├── test_db.py / test_ingest.py / ...
    └── integration/
        ├── test_eval_bank_coverage.py
        └── test_eval_ranker_recall.py
```

---

## Working conventions

- **Run tests with `.venv/bin/python -m pytest tests/ -q`.** All tests
  (currently ~91) must pass before merging.
- **Don't bypass `sable_kol.db`** for `kol_candidates` writes — the JSON
  encoding, partial-unique-index handling, and conflict routing live there.
- **All Anthropic clients are injectable.** Tests pass a fake; production
  builds the real client lazily inside `run_classify` / `run_find`.
- **`socialdata_fetcher` is injectable** in `build_external_profile`. Default
  imports from Slopper; tests pass a stub.
- **No new SQL DDL in this repo.** If you need a new column, add a migration
  to SablePlatform.

---

## Common tasks

| Goal | How |
|------|-----|
| Re-run all tests | `.venv/bin/python -m pytest tests/ -q` |
| Add a new candidate row | `from sable_kol.db import upsert_candidate` (do NOT raw INSERT) |
| Add a new cost event | `from sable_kol.cost import record` |
| Read a project profile | `build_org_profile(conn, org_id)` or `build_external_profile(...)` |
| Skip eval tests in dev | `SABLEKOL_SKIP_EVAL=1 pytest` |
| Bump recall threshold | `SABLEKOL_RECALL50_THRESHOLD=0.6 pytest` |

---

## Phasing

- **Phase 0 (this repo, current):** CLI only, manual list export, free
  matcher, gold-set eval. Adapter stub in SablePlatform but not in cron.
- **Phase 2 (next):** Slopper meta-watchlist sync, programmatic Cahit
  refresh, SableWeb KOL page, manual relationship-tagging UI.
- **Phase 3+:** Cross-community talent sourcing, outreach actions,
  NL queries, paid-promo (a) layer, real cross-org entity resolution.

See `PLAN.md` § Phasing for the full split.
