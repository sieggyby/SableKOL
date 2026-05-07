# SolStitch outreach plan — Phase 2/3 results (2026-05-06)

## Phase 2 — audience extraction

Three brand audiences pulled via `/twitter/followers/list` (per-page = 49,
verified by Phase 0.5 contract spike):

| Audience | Handle | id_str | Pages | Profiles kept (≥500 followers) | Cost logged |
|----------|--------|--------|------:|------------------------------:|------------:|
| Doji | `@doji_com` | 1874926744890658816 | 115 | 2,218 | $0.230 |
| 9dcc | `@9dccxyz` (plan said `@9dcc` — wrong, that handle has 12 followers) | 1517184941888614400 | 578 | 9,856 | $1.156 |
| Fabricant | `@thefabricant` | 1053725893666004993 | 567 | 7,851 | $1.134 |
| **Total** | | | **1,260** | **19,925** | **$2.520** |

* Wall time: 32 minutes sequential
* Estimate was $2.56 (62.7K profiles ÷ 49/page × $0.002); actual $2.52, close to estimate
* All three runs `cursor_completed=1` (no partials)
* Output JSONL files under `grok_responses/audience_*_2026_05_06.jsonl`
* All edges persisted to `kol_follow_edges` (run records in `kol_extract_runs`)

## Phase 3 — ingest + dedupe

Ran `scripts/ingest_audiences.py` on the three JSONL files:

| Audience | Parsed | Inserted | Updated | Conflicts |
|----------|-------:|---------:|--------:|----------:|
| doji_audience | 2,218 | 2,125 | 92 | 1 |
| 9dcc_audience | 9,856 | 9,526 | 330 | 0 |
| fabricant_audience | 7,851 | 6,536 | 1,314 | 1 |

**Cross-source overlap:**
* Unique handles across all 3: **18,478** (1,447 dedupe vs raw 19,925)
* In ≥2 audiences (multi-source vote): **1,365**
* In all 3 audiences (triple vote): **82**

**Phase 6 candidate gates** (post-ingest, pre-classify):

| Gate | Cohort size |
|------|-----------:|
| `following<1000` (loose) | 2,795 |
| `following<1000 AND followers≥10K` (variant 2) | **207** |
| `following<500 AND followers≥5K` (tight) | 152 |

The variant-2 gate naturally produces a Phase-4-sized cohort (~200) without
manual review. Phase 6 cost on that cohort: ~$4.22 logged (well under the
$50 ceiling).

## Phase 3 — classify (in flight)

Running Haiku archetype/sector tagger on 18,631 unclassified rows. Estimated
cost ~$9-10. Expected wall time 1-2 hours given Anthropic rate limits.
After completion: re-score via `enrich --score-only`, then run
`scripts/phase6_extract_followings.py --dry-run` to confirm the
sector-filtered variant-2 cohort size.

## Cost rollup so far

| Phase | Estimate | Actual logged |
|-------|---------:|--------------:|
| 0.5 (spike) | $0.50 | $0.03 |
| 2 (audiences) | $2.56 | $2.52 |
| 3 ingest | $0 | $0 |
| 3 classify | $9-10 | _in flight_ |
| **Subtotal** | **$12-13** | **$2.55 + classify** |
| Plan ceiling | $37 | — |

Spike + audiences came in at $2.55 — about 7% of the $37 plan ceiling. With
classify factored in we'll land ~$12-13 by end of Phase 3, leaving ample
headroom for Phase 6.
