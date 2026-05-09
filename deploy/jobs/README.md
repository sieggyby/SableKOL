# SableKOL job-runner systemd units

Companion to the `regenerate/` units. These run the **any-project KOL wizard**
worker (`sable-kol jobs run`) on a 60-second timer.

## What it does

Every 60 seconds (with 0-10s jitter), `sable-kol-jobs.service` fires once and:

1. Connects to `sable.db` (Postgres in prod, via `SABLE_DATABASE_URL`).
2. Calls `claim_next_job(job_type='kol_create', worker_id=<uuid>)` — atomic
   claim of one pending job, or stale-reclaim of a crashed worker's running
   job (10-min default cutoff).
3. Walks the claimed job's `job_steps` rows in order: enrich → suggest_comparable
   → reuse_check → survey_cohort_<handle> × N → write_yaml → regenerate.
4. Persists each step's output via `complete_step` so resumes after a crash
   are idempotent.
5. On all-success, marks the job `done`. On retry-budget exhaustion, marks
   the job `failed`. On a deferred step (xAI 429), releases the job back to
   `pending` and exits — the next tick picks it up.

If there's nothing to claim, the tick exits in milliseconds.

## Install

```bash
# 1. Drop the env file (root-owned, sable-readable).
sudo install -d -m 0750 /etc/sable
sudo tee /etc/sable/sable-kol-jobs.env >/dev/null <<'EOF'
XAI_API_KEY=xai-...
ANTHROPIC_API_KEY=sk-ant-...
SABLE_DATABASE_URL=postgresql://sable:...@127.0.0.1:5432/sable
EOF
sudo chmod 0600 /etc/sable/sable-kol-jobs.env
sudo chown root:sable /etc/sable/sable-kol-jobs.env

# 2. Copy the units.
sudo cp deploy/jobs/sable-kol-jobs.service /etc/systemd/system/
sudo cp deploy/jobs/sable-kol-jobs.timer   /etc/systemd/system/

# 3. Enable + start the timer.
sudo systemctl daemon-reload
sudo systemctl enable --now sable-kol-jobs.timer

# 4. Watch ticks land.
journalctl -u sable-kol-jobs.service -f
```

Pairing with the existing `sable-kol-regenerate@<client>.timer` units is fine
— they read different env files and don't touch the same DB rows.

## Why a separate `XAI_API_KEY` + `ANTHROPIC_API_KEY`

The wizard worker calls Grok (xAI) for the `enrich` and `suggest_comparable`
steps and Haiku (Anthropic) inside the `regenerate` step's classify pass.
The Grok sidecar container (`SableWeb/docker-compose.yml`) gets `XAI_API_KEY`
in *its* env; the host worker also needs it because `sable_kol.grok_api`
runs in-process on the host, NOT inside the sidecar.

## SocialData budget

Each `survey_cohort_<handle>` step writes one `cost_events` row per page
(`$0.002` estimated, `~3x` actual per the `feedback_cost_estimate_framing.md`
memory). The wizard's `cost_ceiling_usd` and per-operator daily quota are
enforced at submit time (Phase D — wizard route handler) so the worker
trusts what it claims. If the operator-set ceiling is wrong, the worker will
keep spending — there's no per-step cost gate.

## Stale-reclaim

A claimed job that hasn't been updated in 10 minutes is re-claimable by
another worker. This is how a crashed worker's job gets recovered: when the
sable-kol-jobs.timer fires after the crash, `claim_next_job` finds the row
with `status='running'`, `worker_id=<dead worker>`, `updated_at < now-10min`
and re-claims it under the new tick's worker_id.
