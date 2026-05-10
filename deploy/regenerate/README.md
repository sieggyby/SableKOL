# SableKOL regenerate timer (Hetzner)

Daily systemd timer that refreshes outreach + network-graph deliverables
for one client. No SocialData spend (audit finding #1 — that path
requires a writable web mount + budget guard, deferred to Phase 2).

## What it does

For each enabled client (`solstitch`, `tig`, etc.), once per day:

1. Classify any unclassified candidates (Anthropic Haiku — bounded cost)
2. Recompute `kol_strength_score`
3. Build the outreach plan — writes `<client>_<doc>_<date>_<mode>.{md,json,csv}`
   plus `_full` variants and `latest_<mode>_<doc>.{ext}` symlinks
4. Build the network graph — writes `<client>_network_<date>_interactive.{gexf,html}`

All artifacts land in `/opt/sable/outreach/<client_id>/`. SableWeb's
download API serves files from this dir + merges live operator-tag state
at request time.

## Files

| File | Role |
|---|---|
| `sable-kol-regenerate@.service` | Parameterized service (one instance per client_id) |
| `sable-kol-regenerate@.timer` | Daily 03:00 UTC, paired with the service |
| `README.md` | This file |

The `@.` template syntax means each enabled timer gets its own instance
keyed by client_id — `sable-kol-regenerate@solstitch.timer` is distinct
from `sable-kol-regenerate@tig.timer`.

## Install (Hetzner CX21 production)

Pre-requisites:

* SableKOL editable-installed at `/opt/sable/sable-kol/` with venv at
  `.venv/` (matches the existing `deploy/DEPLOY.md` pattern from Slopper)
* SablePlatform at migration head (39 as of 2026-05-07)
* Per-client YAML configs at `/opt/sable/clients/<id>.yaml` (read-only mount
  for SableWeb container; read-write for SableKOL CLI)
* Outreach dir at `/opt/sable/outreach/` (writable)
* `sable` user/group with appropriate ownership of those paths
* `ANTHROPIC_API_KEY` and `SABLE_DATABASE_URL` exported in the service
  environment (edit the `Environment=` lines in the .service file before
  installing — DON'T commit secrets)

Steps:

```bash
# 1. Edit Environment= lines in sable-kol-regenerate@.service to point at
#    your real Postgres URL + Anthropic key. (TODO: switch to a sourced
#    EnvironmentFile= once we stand up secret management.)
sudo cp sable-kol-regenerate@.service /etc/systemd/system/
sudo cp sable-kol-regenerate@.timer   /etc/systemd/system/
sudo systemctl daemon-reload

# 2. Per-client enable
sudo systemctl enable --now sable-kol-regenerate@solstitch.timer
# Repeat for each YAML in /opt/sable/clients/

# 3. Verify
systemctl list-timers | grep sable-kol-regenerate

# 4. Tail the next run (or trigger one manually)
journalctl -u sable-kol-regenerate@solstitch.service -f
sudo systemctl start sable-kol-regenerate@solstitch.service   # ad-hoc
```

## Logs

Each run prints a JSON summary to stdout (captured by journald). Sample:

```json
{
  "client_id": "solstitch",
  "started_at": "2026-05-07T03:00:14+00:00",
  "finished_at": "2026-05-07T03:08:42+00:00",
  "duration_seconds": 508.3,
  "classify_skipped": false,
  "classify_classified": 142,
  "classify_dropped": 8,
  "classify_cost_usd": 0.078,
  "score_rescored": 12943,
  "outreach_files_written": [
    "solstitch_report_2026-05-07_stealth.json",
    "solstitch_report_2026-05-07_stealth.md",
    "solstitch_leads_2026-05-07_stealth.json",
    "solstitch_leads_2026-05-07_stealth.csv",
    "..._full variants..."
  ],
  "network_files_written": [
    "solstitch_network_2026-05-07_interactive.gexf",
    "solstitch_network_2026-05-07_interactive.html"
  ],
  "errors": []
}
```

`errors` non-empty → service exits 1 → systemd journal flags the unit as
failed. `journalctl -u sable-kol-regenerate@<id> --since today -p err` to
filter.

## Disabling

```bash
sudo systemctl disable --now sable-kol-regenerate@solstitch.timer
```

## Why daily and why 03:00

* **Daily** — bank state changes weekly at most (audience extractions
  + classify happen on operator command). Daily refresh keeps the
  deliverables fresh while costing ~$0.10-0.50/run for Haiku-only spend.
* **03:00 UTC** — well after USA-east evening traffic, well before EU
  morning. Avoids contending with Slopper's weekly cycle (Mon 06:00 UTC).
  Adds a 0-300s `RandomizedDelaySec` jitter so timers across clients
  don't fire simultaneously.

## Cost expectations

Per regenerate run (no SocialData):

| Step | Cost |
|---|---|
| Classify (only unclassified) | ~$0.05-0.50 (depends on backlog) |
| Score recompute | $0 |
| Outreach plan generation | $0 |
| Network graph render | $0 |

Per-client per-day average: **~$0.10-0.20** in steady state once
classify has settled. Anthropic Haiku invoice rolls into the existing
SableKOL `cost_events` table — visible via `sable serve`'s cost endpoints.

## What this DOES NOT do

* No SocialData calls. Adding new audience pulls or following pulls
  requires explicit operator action via `sable-kol bulk-fetch
  followers/following --client <id>`.
* No tag bake-in. Operator tags merge at SableWeb request time, not at
  Python regenerate time (audit finding #2).
* No `regenerate` web endpoint. Re-adding requires writable mount +
  advisory lock + budget guard + admin role check (audit finding #1).
* No multi-tenant safety beyond `client_id` filter on
  `kol_extract_runs`. TIG operators running concurrent regenerates of
  TIG won't disturb a SolStitch run, but two operators triggering
  regenerate on the same client at the same second WILL race on
  `latest_*` symlink updates. Mitigate by trusting the cron schedule
  and SSH-only ad-hoc runs.

## Cadence + freshness contract (KO-3)

Each regenerate writes `_meta.generated_at_utc` (ISO-8601-Z) into the
report.json and leads.json payloads. The SableWeb `/draft-intro` route
reads this as input-freshness when assembling a per-candidate cold-intro
context. Practical implications:

* **A daily timer keeps the freshness ≤ 24h** for every enabled client.
  If you disable a client's timer, drafts against that client will surface
  a stale freshness line in the UI but still work.
* **Pre-KO-3 leads files have no `_meta.generated_at_utc`.** SableWeb
  falls back to the file's mtime and flags the response as
  `approximate=true`. The deploy step (`SIDECAR.md` § "Forced regenerate")
  loops over every known client to backfill canonical timestamps; do this
  once per deploy that bumps SableKOL.
* **Manual ad-hoc regenerates are fine** — the timestamp is rewritten on
  every run, so an operator-triggered run at 14:00 UTC will land canonical
  freshness for the rest of the day.
