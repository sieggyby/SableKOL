# SableKOL — TODO

**Scope:** SableKOL × SablePlatform × SableWeb intersection. NOT the global "what's next" tracker — for that, see `~/Projects/Sable_Slopper/TODO.md`.

**For shipped work**, see [`docs/AUDIT_LOG.md`](docs/AUDIT_LOG.md).
**For design rationale of the any-project wizard**, see [`docs/any_project_wizard_plan.md`](docs/any_project_wizard_plan.md).

**Last updated:** 2026-05-10 — KO-3 + KO-1.b shipped end-to-end (SableKOL Phase 0–2 + SableWeb Phase 3 + deploy/docs Phase 4). See `docs/AUDIT_LOG.md` for the full landing entry. Only KO-4 (paid bank source expansion) remains open.

---

## Open

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

KO-3-specific out-of-scope (preserved post-ship):
- Auto-send to X / DM. Strictly operator-assisted; drafts are read-only.
- Multi-candidate batch drafts.
- Persona-tuning UI (priming is code-edited via `sable_kol/persona_priming.py`).
- Saving drafts server-side (ephemeral; copy-paste).
- Cross-persona drafting (locked to the logged-in operator's own persona).
- Live-X-research mode (deliberately rejected on cost + reliability grounds).
- Candidate-level draft history table.

---

## When in doubt

- For wizard-architecture questions, re-read `docs/any_project_wizard_plan.md`.
- For shipped work, `docs/AUDIT_LOG.md`.
- For graph-reuse architectural framing, memory file `project_sablekol_graph_reuse.md`.
- For cost-estimate calibration, memory file `feedback_cost_estimate_framing.md`.
- For KO-3 implementation surface, memory file `project_sablekol_ko3_shipped.md`.
