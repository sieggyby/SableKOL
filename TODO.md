# SableKOL — TODO

**Scope:** SableKOL × SablePlatform × SableWeb intersection. NOT the global "what's next" tracker — for that, see `~/Projects/Sable_Slopper/TODO.md`.

**For shipped work**, see [`docs/AUDIT_LOG.md`](docs/AUDIT_LOG.md).
**For design rationale of the any-project wizard**, see [`docs/any_project_wizard_plan.md`](docs/any_project_wizard_plan.md).

**Last updated:** 2026-05-09 — wizard live in prod; KO-1 (preflight context flags) shipped in `6551693`, KO-2 (KOLNetwork zoom + pan) shipped in SableWeb `af8dbbe`. See audit log for the full picture of what's live.

---

## Open

### KO-1.b — Sidecar passthrough for preflight context flags

Code shipped in `6551693`: `enrich_handle` / `suggest_comparable_projects` / `build_preflight_response` all accept `context` / `exclude_handles` / `allow_non_crypto_research` kwargs. CLI exposes them as `--context` / `--exclude-handles` / `--allow-research`. **The sidecar service (`preflight_service.py`) does NOT yet plumb them through**, so the wizard UI can't reach them.

**Acceptance:**
- `preflight_service.py` `/preflight` and `/suggest-comparable` request models (Pydantic) gain `context: str | None = None`, `exclude_handles: list[str] | None = None`, `allow_non_crypto_research: bool = False`.
- Endpoint forwards them into `build_preflight_response` / `suggest_comparable_projects`.
- SableWeb `src/lib/kol-create-schemas.ts` Zod schemas mirror the new optional fields.
- SableWeb wizard Step 1 ("Identify") gains a small "Project context" textarea + an "Allow research/AI peers" checkbox; advanced collapsed area for `exclude_handles`.
- Tests in `tests/test_preflight_service.py` cover the three new fields end-to-end.

Defer if KO-3 lands first, since KO-3 needs a similar sidecar-side touch and we can bundle.

### KO-3 — Per-candidate Grok enrichment button

**Memory:** `project_sablekol_grok_enrichment.md`.

**Why:** today's outreach motion still routes through CSV exports + manual cold-intro authoring. The bank has rich per-candidate signal (sources, archetype, sector, axis scores, cluster) but operators don't see it as a usable cue when writing intros — they see it as a data point. A persona-conditioned Grok call closes that loop: the operator clicks "Draft intro" on a candidate row and gets a 2-3 line opener that already references concrete signal from the bank, in the operator's voice register.

**User flow:**
1. Operator on `/ops/kol-network/<slug>` (SableWeb).
2. Clicks a candidate node → drawer opens (existing UI, KO-2 shipped).
3. New button: "Draft cold-intro (Grok)". Adjacent persona dropdown defaults to the logged-in operator (Sieggy / Sparta / Arf).
4. Click → spinner → Grok returns. Drawer renders the result with a freshness timestamp + `signal_metadata` chip (per AGENTS interpretive-signal taxonomy), a copy-to-clipboard button, and a "regenerate" link.
5. Output is NOT auto-sent anywhere. This is operator-assist, not auto-outreach.

**Architecture (mirrors the existing wizard sidecar pattern — no new infra):**
- `sable_kol/grok_api.py` — new `draft_cold_intro(handle, persona, project_context, candidate_signal) -> ColdIntroDraft`. Pydantic-validated. Same retry/backoff lineage as `enrich_handle`.
- `sable_kol/preflight_schemas.py` — new `ColdIntroDraft` (intro_text, suggested_angle, signal_metadata) + `ColdIntroRequest`.
- `sable_kol/preflight_service.py` — new `POST /draft-intro` endpoint, gated by the same `secrets.compare_digest` token, taking `{handle, persona, project_slug, candidate_id?}`. Persona is one of `sieggy | sparta | arf` (extensible via `PERSONAS` constant); each persona has a small priming block (voice register, opening style, what they avoid).
- `sable_kol/persona_priming.py` (new) — the persona priming blocks. Three profiles initially. Plain Python dict keyed by persona slug; no DB table needed at v1.
- SableWeb `src/app/api/ops/kol-network/draft-intro/route.ts` — proxies to sidecar with Zod validation, gated by `withWizardGate()` (same allowlist + audit pattern as the wizard create routes), submitter-or-admin rule.
- SableWeb `src/components/ops/KOLCandidateDrawer.tsx` — adds the button + persona dropdown + result panel. Lives next to the existing relationship-tagging UI.
- SableWeb `src/lib/kol-create-schemas.ts` — `ColdIntroDraftSchema` Zod mirror.

**Cost guardrails:**
- Hit Grok once per click. No streaming; complete-or-fail.
- Daily quota: 50 drafts/operator/24h, counted from `kol_create_audit` outcome='draft_intro_allowed' rows. Reuses the existing audit table — add a new outcome value, no migration.
- Per-call cost: ~$0.002 xAI (operator-tier model = grok-4-latest). Daily ceiling per operator: ~$0.10. Same envelope as the wizard preflight.

**Test cohort:** SolStitch top-100 (already curated). Validate that drafts:
- reference at least one concrete signal from the bank (not generic).
- match the persona register (terse Sieggy vs. warmer Sparta vs. technical Arf).
- carry the `signal_metadata` block + freshness timestamp.

**Acceptance gates:**
- 6+ tests in `tests/test_grok_api.py` covering happy path + 3 personas + missing-signal fallback + xAI 503 retry.
- 4+ tests in `tests/test_preflight_service.py` covering token gate + happy path + invalid persona + audit row written.
- 3+ tests in SableWeb `tests/api-kol-draft-intro.test.ts` covering anonymous→401, non-allowlisted→403, allowed→200 with audit row.
- Manual: 5 SolStitch top-100 candidates → 5 drafts × 3 personas = 15 drafts. Smell-test the variance.

**Out of scope for v1:**
- Auto-send to X / DM. Strictly operator-assisted draft generation.
- Multi-candidate batch drafts (loop in next iteration if useful).
- Persona-tuning UI. Persona blocks are code-edited at v1.
- Saving drafts to a DB. Drafts are ephemeral; copy-paste is the persistence.

**Phasing (estimated ~1 day total):**
- Phase 1 (~3h): `grok_api.draft_cold_intro` + `persona_priming.py` + tests. Land standalone CLI verb `sable-kol draft-intro` for direct verification before the sidecar wiring.
- Phase 2 (~2h): sidecar `/draft-intro` endpoint + tests. Bundle KO-1.b sidecar passthrough into the same commit if not already shipped.
- Phase 3 (~3h): SableWeb route + drawer button + Zod schema + tests.
- Phase 4 (~1h): manual SolStitch top-100 smell-test, prod deploy.

**Defer until:** operator says "go" or active outreach surfaces a clear bottleneck. KO-1.b is a soft prerequisite — if not done, do it as part of Phase 2 above.

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
