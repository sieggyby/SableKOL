# SableKOL — TODO

**Scope:** SableKOL × SablePlatform × SableWeb intersection. NOT the global "what's next" tracker — for that, see `~/Projects/Sable_Slopper/TODO.md`.

**For shipped work**, see [`docs/AUDIT_LOG.md`](docs/AUDIT_LOG.md).
**For design rationale of the any-project wizard**, see [`docs/any_project_wizard_plan.md`](docs/any_project_wizard_plan.md).

**Last updated:** 2026-05-09 — wizard live in prod, post-launch tactical fixes shipped (model bump, timeouts, cost rebase, datetime portability). See audit log for the full picture.

---

## Open

### KO-1 — Commit SableKOL preflight context flags

Local-only changes to `sable_kol/{cli.py,grok_api.py}`:
- `--context` / `--exclude-handles` / `--allow-research` flags on `sable-kol preflight` for non-fashion/web3 clients (TIG-style DeSci/AI projects)
- 5 new axes added to `FIXED_AXIS_LIBRARY`: `research-academic`, `ai-ml`, `desci-science`, `algorithmic-quant`, `e-acc-frontier`
- Was useful in this session for handling research-leaning preflight runs; should ship before next operator-driven preflight.

**Acceptance:** new flags surface in `sable-kol preflight --help`; new axes available to wizard Step 2; tests updated to cover the new keyword args path through `enrich_handle` / `suggest_comparable_projects` / `build_preflight_response`. (`tests/test_preflight_cli.py` is already partially updated to accept `**kwargs` — finish the surface-area coverage before commit.) Sidecar service surface needs the same flag passthrough so the wizard UI can reach them — will require a Phase B follow-up commit (preflight_service.py + Zod schema + wizard step).

### KO-2 — Commit SableWeb KOL bits

Local-only in SableWeb (held out of Phase A intentionally — this is the cleanup):
- `src/components/ops/KOLNetwork.tsx` — zoom + pan + `hideUnscored` default-on toggle (already deployed via rsync 2026-05-07, not yet committed)
- `src/lib/allowlist.ts` — client allowlist swap: `client@psy.xyz` → `client@solstitch.xyz`
- `tests/kol-create-allowlist.test.ts` — matching test fixture (negative-case email)

**Note:** SableWeb working tree also has unrelated work (intake form, multisynq proof, synq pages, db.ts eslint cleanup, checkpoint-reader doc fix, plus the same `psy_protocol` → `multisynq`/`solstitch` substitution across 7 test files + TODO.md). Those are not KOL-intersection concerns; commit them in their own non-KOL commit.

### KO-3 — Per-candidate Grok enrichment button (planned)

Memory: `project_sablekol_grok_enrichment.md`.

Operator-facing button on `/ops/kol-network/<slug>` candidate detail. Generates a 2-3 line cold-intro note for the selected candidate, conditioned on operator persona (Arf / Sparta / Sieggy). Reuses the existing sidecar — new endpoint `/enrich-candidate` taking `{handle, persona, project_context}` → Grok prompt → returns `{intro_note, suggested_angle, freshness_metadata}`.

**Test cohort:** SolStitch top-100 (already curated).

**Defer until:** outreach status tracking lands or operator demand surfaces. Currently no active outreach motion needs this.

### KO-4 — Bank source expansion (paid)

Bank is mined to depth on existing sources for Prometheus-fit candidates (TIG-follower low_signal sweep returned 1 weak hit). Future Prometheus-style outreach needs new graph sources. Cost ~$0.20-0.50 per fetch. Last SocialData balance ~$23.76 (memory `project_socialdata_budget.md`).

Candidates:
- Demis Hassabis followings (direct AI-research density)
- Geoffrey Hinton followings
- Stephen Boyd followings (Stanford optimization — would surface convex-optim folks)
- Andrew Ng retry (earlier fetch returned 9 rows — possible privacy quirk)
- Second-degree fetches off Tier-1 outreach targets (e.g. @aaron_defazio)

**Defer until:** new Prometheus-class event or operator decision to push deeper.

---

## Intentionally not in scope

Preserved from the v3 wizard plan — do not get pulled in:

- Generalized cross-client `cohort_pool` abstraction (per-survey reuse only — see memory `project_sablekol_graph_reuse.md` for the framing).
- Per-vertical decay strategy (180-day window is one global knob).
- Cancel-running-job button (operator must wait or SSH).
- Cross-operator job visibility (siloed by submitter except for admin override).
- Bulk job operations.
- Self-serve client login.
- Operator-typed custom axes (fixed library only — KO-1 expands the library, doesn't open it).
- Slack/email completion notifications (in-app status page only for v1).
- "Delete project" web flow.
- Bulk tag/cohort editing.

---

## When in doubt

- For wizard-architecture questions, re-read `docs/any_project_wizard_plan.md`.
- For shipped work, `docs/AUDIT_LOG.md`.
- For graph-reuse architectural framing, memory file `project_sablekol_graph_reuse.md`.
- For cost-estimate calibration, memory file `feedback_cost_estimate_framing.md`.
- The plan's "Codex audit response" tables map every Codex finding to its v3 fix.
