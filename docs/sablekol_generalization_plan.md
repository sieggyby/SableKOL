# SableKOL generalization — multi-client, stealth-mode, filter discipline

**Date:** 2026-05-07
**Status:** Plan; partial execution in this session.
**Scope:** Generalize SableKOL's network-graph + outreach-plan tools so they
work for any Sable client, with per-client config, stealth/post-launch
strategy modes, and disciplined org/celebrity filters.
**Companion plan:** `sableweb_kol_network_plan.md` — frontend integration.

---

## Why this needs to happen

The SolStitch run produced a working but **single-tenant** pipeline:

| Step | Where the SolStitch context is hardcoded |
|---|---|
| Audience extraction | `scripts/ingest_audiences.py` — Doji/9dcc/Fabricant handles literal |
| Curator weights | `~/.sable/kol_list_curators.yaml` — `doji_audience: 2.0` etc. |
| Outreach-plan filter | `scripts/build_outreach_plan.py` — manual pins `{zigor, toomuchlag, …}` literal |
| Network-graph denylist | `scripts/build_network_graph.py` — `ORG_DENYLIST` is global, not per-client |
| Tiering thresholds | `outreach_plan.py` — `100K / 10K / 1K` literal, ignores stealth-vs-public |
| Mission/vibe scoring | absent — sector tags are coarse, not theme-aligned |
| Celebrity / whale filter | absent — Elon Musk, CZ, Vitalik appear in deliverables |

Three other Sable clients are in the pipeline (TIG, Multisynq, PSY Protocol)
plus the prospect lane (Flow, future). The next time we run this on a client
we should be able to swap a config file rather than fork the scripts.

---

## Operator strategy: stealth vs post-launch

The SolStitch run revealed an embedded assumption: **tier by reach descending**.
Tier-A = 100K+, Tier-B = 10K-100K, Tier-C = 1K-10K. This is wrong for a
project in **stealth pre-launch** like SolStitch.

### Stealth-mode targeting (new)

A pre-launch project has no story to amplify yet — it has a thesis, a debut
window, and an opportunity to seed taste-makers who will adopt early. The
high-leverage cohort:

* **Sub-15K-follower micro-influencers with high penetration into bigger
  KOLs' following graphs.** These are the people whose follow signals upstream
  to the next tier — the curators that the curators read. They engage with
  DMs. They aggregate attention.
* **People that fit the project's mission/vibe.** Sector tags are too coarse.
  We need to score on theme overlap.
* **NOT** Vitalik / CZ / Elon / Saylor / large-broadcast accounts. These have
  100K-100M followers, follow ~50-500 people, do not read DMs from random
  projects, and do not "shill" anything that didn't pay them or move their
  PnL. Including them in outreach is operator-time waste.

### Post-launch / public-mode targeting (existing)

A project past launch with traction can absorb mid-tier and selective Tier-A
outreach. Existing reach-descending tiering is appropriate. Stealth mode and
post-launch mode are different presets, not different code.

### How "stealth" reverses the tier definitions

| Tier | Stealth mode (pre-launch) | Public mode (post-launch) |
|---|---|---|
| A | followers ≤ 15K, in_pool ≥ 6, vibe-fit ≥ 0.5, reachable | followers ≥ 100K, sector match, brokers mapped |
| B | followers 15K-50K, vibe-fit ≥ 0.4 | followers 10K-100K, sector match |
| C | followers 1K-15K, vibe-fit ≥ 0.3 | followers 1K-10K, templated |

`in_pool` (follow-graph centrality) is the dominant signal in stealth.
Reach is the dominant signal in public mode.

---

## Plan

### Phase 1 — Immediate (this session)

1. **Org filter on outreach plan.** Reuse `is_organization` from
   `build_network_graph.py` (move to a shared `sable_kol/filters.py`),
   apply at outreach-plan generation. Org-toggle in the script.
2. **Celebrity / whale filter.** Hand-curated `CELEBRITY_DENYLIST` plus
   heuristic (followers > 5M AND friends/followers ratio > 100 → whale,
   broadcast-only). Apply to outreach plan and network graph.
3. **Stealth-mode tiering.** New `--stealth-mode` flag on
   `scripts/build_outreach_plan.py` that inverts the tier thresholds per the
   table above. SolStitch should default to `--stealth-mode`.
4. **Cheap vibe-fit score.** Theme-keyword overlap between the project's
   themes and each candidate's bio. 0..1 score, written to the
   `OutreachTarget`. Tier-A in stealth requires vibe-fit ≥ 0.5.
5. **Apply org+celebrity filter to network graph and outreach plan in
   parallel.** Both deliverables consistent.

### Phase 2 — Per-client configuration (next session, ~1 day)

Move per-client constants out of scripts into config files at
`~/.sable/clients/<client_id>.yaml`:

```yaml
# ~/.sable/clients/solstitch.yaml
client_id: solstitch
display_name: SolStitch
mode: stealth                       # stealth | public
debut_date: 2026-05-28
sector_focus: [fashion, culture, art, design, nfts, streetwear]
themes:
  - RWA fashion
  - tokenized redemption
  - on-chain royalties
  - phygital
  - creator economy
audiences:                          # Phase 2 audience-extraction targets
  - { handle: doji_com,    label: doji_audience,      curator_weight: 2.0 }
  - { handle: 9dccxyz,     label: 9dcc_audience,      curator_weight: 2.0 }
  - { handle: thefabricant, label: fabricant_audience, curator_weight: 1.8 }
manual_pins:
  - zigor
  - toomuchlag
  - nanixbt
  - auri_0x
  - loomdart
org_denylist_extras: []             # client-specific orgs to exclude
person_allowlist_extras: []         # client-specific overrides
celebrity_denylist_extras: []
tier_thresholds:
  stealth:
    A: { max_followers: 15000, min_in_pool: 6,  min_vibe: 0.5 }
    B: { max_followers: 50000, min_vibe: 0.4 }
    C: { max_followers: 15000, min_vibe: 0.3 }   # overlaps tier A by reach but distinguishes by vibe-fit
  public:
    A: { min_followers: 100000 }
    B: { min_followers: 10000 }
    C: { min_followers: 1000 }
```

Refactor scripts:
- `build_outreach_plan.py --client solstitch` → reads `~/.sable/clients/solstitch.yaml`
- `build_network_graph.py --client solstitch` → same
- `ingest_audiences.py --client solstitch` → reads audience handles from YAML

Shared loader: `sable_kol/client_config.py` returning a typed `ClientConfig`
dataclass.

### Phase 3 — Vibe-fit upgrade (~2 days)

Cheap keyword-overlap is a placeholder. Replace with one of:

a. **Haiku semantic scoring** — single Haiku call per candidate scores
   `bio + display_name + sector` against a curated `themes` paragraph from
   client config. Returns 0..1. Cost: ~$0.0005/candidate × 200 = $0.10/run.
   Pro: leverages existing `match.py` infra. Con: another LLM call.

b. **Embedding-based** — compute one embedding per candidate (cached) and
   cosine-similarity against the project themes embedding. Cost: ~$0.001
   per 1K candidates one-time, free on re-run. Pro: scalable, free re-runs.
   Con: new Anthropic embeddings model dependency.

Pick (b) — caches well, generalizes across clients, no per-run LLM cost.

### Phase 4 — Cross-client comparison views (~half-day)

Once N≥3 clients have run, the operator wants:
- **Cross-client kingmakers** — accounts followed by cohort across multiple
  clients. These are the universal taste-makers across the Sable book.
- **Audience overlap** — handles in the cohort/candidate sets of multiple
  clients. The operator can spot an "in-house ambassador" who fits more than
  one project.
- **Bank coverage by client** — which clients have classified, enriched, etc.
  data, and where the gaps are.

These are SableWeb views (next plan).

### Phase 5 — SableKOL `client` CLI commands (~half-day)

```
sable-kol client list                               # all configured clients
sable-kol client show solstitch                     # config + cohort sizes + last-run timestamps
sable-kol client run solstitch [--phase audiences|classify|follow-graph|outreach]
sable-kol network export solstitch --output ...     # GEXF + interactive HTML
sable-kol outreach build solstitch [--mode stealth|public]
```

---

## Filter discipline (the celebrity + org problem)

### Three filters, applied in order

```
candidates → drop is_organization → drop is_celebrity → drop is_unreachable
            → keep person_allowlist (overrides)
```

### `is_organization`
Already implemented in `build_network_graph.py`. Move to
`sable_kol/filters.py`. Three layers (denylist, handle suffix, classifier
archetype, bio first-person-plural). Override list for false positives.

### `is_celebrity`
**New.** Two layers:

```python
CELEBRITY_DENYLIST = {
    # Crypto-OG broadcast accounts that don't shill
    "elonmusk", "vitalikbuterin", "cz_binance", "saylor", "100trillionusd",
    "apompliano", "kobeissiletter", "9gagceo", "cryptorover", "ashcrypto",
    "justinsuntron", "brian_armstrong", "balajis",
    # Mainstream celebrities outside crypto-native space
    "parishilton", "diplo",  # already surfaced in our top-25
}

def is_celebrity(handle, followers, friends_count):
    if handle.lower() in CELEBRITY_DENYLIST:
        return True
    # Heuristic: 1M+ followers AND follow:follower ratio < 0.005 → broadcast-only
    if followers >= 1_000_000 and friends_count and (friends_count / followers) < 0.005:
        return True
    return False
```

Note the **edge case**: some 1M+ accounts ARE reachable (e.g.,
@kevinrose 1.5M, follows 4K — ratio 0.0027, would be flagged). Tune the
threshold or maintain an allowlist. Initial config: ratio < 0.0005 (more
conservative) plus the denylist.

### `is_unreachable`
Optional broader filter — `protected: true` accounts, accounts where
`status='dropped'` etc. Mostly already filtered upstream.

---

## Concrete deliverables per phase

| Phase | New files | Modified files |
|---|---|---|
| 1 | `sable_kol/filters.py` | `scripts/build_outreach_plan.py`, `scripts/build_network_graph.py` |
| 2 | `sable_kol/client_config.py`, `~/.sable/clients/<id>.yaml` | scripts above + `scripts/ingest_audiences.py` |
| 3 | `sable_kol/vibe_fit.py` | `outreach_plan.py` |
| 4 | `scripts/cross_client_report.py` | — |
| 5 | new CLI subcommands | `sable_kol/cli.py` |

---

## Risks / known gaps

* **Generalization risk:** SolStitch is the only client we've fully run. The
  YAML schema may need iteration when TIG/Multisynq/PSY run.
* **Stealth mode has only one validation point** (SolStitch). The exact
  thresholds (15K/6/0.5) are operator-judgment, not data-driven yet. Plan to
  re-tune after the SolStitch debut window with reply-rate measurement.
* **Vibe-fit Phase 3** depends on embeddings infra not yet wired into Sable.
  Could fall back to Haiku-scoring (Phase 3a) if embeddings spike too long.
* **Cross-client privacy:** TIG and SolStitch may not want their cohorts
  visible to each other. Phase 4 needs an op-only access gate.
