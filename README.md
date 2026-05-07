# SableKOL

Bank-backed KOL discovery and matching for Sable clients and prospects.

Phase 0 precursor to the SableGraph MVP. Takes a project (Sable org or external
Twitter handle), ranks Crypto-Twitter KOLs by fit, and writes the result as
JSON or a terminal table — with a flag where Sable already has a connection.

---

## Install

```bash
git clone <this repo> ~/Projects/SableKOL
cd ~/Projects/SableKOL
python3.14 -m venv .venv
.venv/bin/pip install -e .

# Optional — required only for `find --paid-enrich` and Phase 2 watchlist sync.
# Pulls in Sable_Slopper for its SocialData wrapper.
.venv/bin/pip install -e ".[paid-enrich]"
```

Then make sure SablePlatform's migration 032 has been applied to your
`sable.db`:

```bash
cd ~/Projects/SablePlatform
.venv/bin/sable-platform init   # idempotent — applies all 32 migrations
```

Set `ANTHROPIC_API_KEY` in your env (classify and find both call Haiku).

---

## Quickstart

```bash
# 1. One-shot: scrape Cahit's list manually, save the page HTML
#    (or use a userscript that emits JSON), then ingest.
sable-kol ingest --list-export ~/Downloads/cahit_list_members.html

# 2. Classify the new rows. Haiku tags archetype + sector + status.
#    ~$0.001/candidate. 1500 rows ≈ $1.50 one-shot.
sable-kol classify

# 3. Cross-reference against sable.db entities — populates sable_relationship,
#    folds in Tier-2 voices from existing client communities.
sable-kol crossref

# 4. Find KOLs for an existing Sable org.
sable-kol find --org tig --limit 20

# 4b. Or for an external prospect (no SocialData call by default).
sable-kol find --handle newproject --sector DeFi --themes "yield,rwa"

# 4c. Add --paid-enrich for one cached SocialData profile lookup
#     (~$0.002, TTL 7d).
sable-kol find --handle newproject --sector DeFi --paid-enrich
```

---

## CLI reference

```
sable-kol ingest --list-export <html|json>     ETL Stage 1 — parse X list export
sable-kol classify [--limit N] [--force]       ETL Stage 3 — Haiku archetype + sector tags
sable-kol crossref                             ETL Stage 4 — sable.db join + Tier-2 fold-in

sable-kol find --org <org_id> [--limit N]                          path (i)
sable-kol find --handle <h> --sector <s> [--themes "..."]
              [--paid-enrich] [--refresh-paid] [--limit N]         path (ii)
sable-kol find ... --output json                                   structured output

sable-kol bank stats                                               row counts, source mix
sable-kol bank dump --handle <h>                                   pretty-print one row
sable-kol bank resolve --conflict <id> --action merge|supersede|discard
                                                                   manual collision triage

sable-kol eval [--gold-set eval/gold_set.yaml]                     bank coverage + ranker recall
```

---

## Cost envelope

| Path | Per-query | Notes |
|------|-----------|-------|
| `find --org`                                  | ~$0.05–0.20 | Haiku rationales on top-K=30 |
| `find --handle --sector`                      | ~$0.05–0.20 | Same; no SocialData call |
| `find --handle --sector --paid-enrich`        | ~$0.05–0.20 | + $0.002 SocialData (cached 7d) |
| `classify` (one-shot, 1500 rows)              | ~$1.50      | Haiku batch |

Every paid call writes a `cost_events` row (`tool='sablekol'`).

---

## Pointers

- **`PLAN.md`** — design rationale, audit history, scope cuts vs. SableGraph spec.
- **`CLAUDE.md`** — repo conventions for future Claude sessions.
- **`eval/gold_set.yaml`** — 5 projects × 20 KOLs. Edit to populate.

---

## Phasing

- **Phase 0 (now):** CLI only, free bank, Haiku rationales, gold-set eval.
- **Phase 2:** Slopper meta-watchlist sync, programmatic refresh, SableWeb page.
- **Phase 3+:** Cross-community talent sourcing, outreach actions, NL queries.
