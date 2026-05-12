# Per-candidate enrichment (KO-3 v2.5)

The operator-facing intel feature: click a node in the SableWeb KOL network
viewer, get back structured intel grounded in the candidate's real X posts
plus computed commonality with the operator. Architectural notes here cover
the request lifecycle, the design decisions that aren't obvious from code,
and the failure modes that matter. See `docs/AUDIT_LOG.md` entries for
2026-05-10 through 2026-05-12 for the chronological pivot history (v1
draft-DM → v2 intel-without-real-data → v2.5 current).

---

## Request lifecycle

```
operator clicks candidate node                                       (SableWeb browser)
       │
       ▼
GET  /api/ops/kol-network/[clientId]/enrich-candidate?handle=@x       (SableWeb route)
       │  reads kol_enrichment for (candidate_id, operator_email)
       ▼
   ┌──────────────────┐
   │ cached row?      │── yes ──▶ return enrichment + cache_meta{is_fresh_fetch=false}
   └──────────────────┘
       │ no
       ▼
   404 no_cache  ───────────────▶ UI shows "Pull intel (Grok)" button

operator clicks Pull intel                                           (SableWeb browser)
       │
       ▼
POST /api/ops/kol-network/[clientId]/enrich-candidate                 (SableWeb route)
       │  withWizardGate → quota check → resolve candidate_id → JOIN kol_candidates
       │  → strict Zod re-validation → fetchSidecar
       ▼
POST /enrich-candidate  on sidecar (compose-internal)                 (sable-kol-preflight)
       │  enrich_candidate(handle, persona, project_context, bank_signal)
       │
       ├─▶ fetch_live_signal(handle)                                  (sable_kol/socialdata_live.py)
       │     ├─ GET /twitter/user/<screen_name>           profile + id_str
       │     └─ GET /twitter/user/<numeric_id>/tweets     20 verbatim recent tweets
       │
       ├─▶ cost_mod.record × 2 (profile + tweets pages)               (sable_kol/cost.py)
       │
       ├─▶ _post_chat to xAI Grok                                     (sable_kol/grok_api.py)
       │     prompt = operator_profile_block + bank_signal +
       │              live_profile_block + verbatim tweet block + output rules
       │
       └─▶ Enrichment payload returned (Pydantic-validated)
       ▼
SableWeb route writes kol_enrichment row + audit_log row
       ▼
return {enrichment, cache_meta{is_fresh_fetch=true}}
       ▼
panel renders structured intel + commonality + commentary             (KOLTagPanel.tsx)
```

---

## Why this shape

**Why SocialData, not Grok "live X search."** The KO-3 v2 design assumed
`grok-4-latest` had reliable real-time X access. It does not — verbatim from
the model when asked to read live timelines: *"Unable to access live X
timeline due to lack of real-time internet access."* All v2-era "live X"
prompts were producing confabulated content from training corpus + prompt
context, dressed up as live reads. v2.5 makes the data source explicit:
SocialData is the ground truth, Grok is the interpreter. See `AUDIT_LOG.md`
2026-05-10 entry for the discovery + commit timeline.

**Why SocialData's `/tweets` endpoint takes the numeric id, not the
screen name.** This is a SocialData quirk discovered live mid-deploy.
`/twitter/user/<screen_name>/tweets` returns 404 even for active accounts;
only `/twitter/user/<numeric_id>/tweets` works. `fetch_live_signal` always
chains the profile fetch (which returns `id_str`) before the tweets fetch.
`fetch_recent_tweets(user_id, ...)` takes the numeric id explicitly in its
public API to make this constraint impossible to violate by accident.

**Why GET/POST split.** Cached intel renders instantly on panel open without
billing xAI. The UI auto-fires GET on mount; only an operator-initiated
"Refresh" or "Pull intel" hits POST. This respects the cost ceiling without
sacrificing snappy panel re-opens. The 10/operator/24h quota gates POSTs only.

**Why no fallback to Grok-only when SocialData fails.** That's exactly the v2
confabulation failure mode we killed. Better to surface 503
`socialdata_balance_exhausted` or `live_data_unavailable` to the operator
than ship fabricated intel they might believe.

**Why ben blocks at both layers.** Defense in depth. The SableWeb route
short-circuits before calling the sidecar (saves a roundtrip); the sidecar
also 409s `persona_placeholder` independently so a direct sidecar caller
can't bypass the gate. Either layer can drift; both have to fail for ben
priming to leak.

---

## Schema reference

`sable_kol/preflight_schemas.py` (all Pydantic with `extra='forbid'`; SableWeb
mirrors with Zod `.strict()`):

| Type | Used at | Purpose |
|---|---|---|
| `CandidateBankSignal` | sidecar request body | Whitelisted bank signal (tier, cluster, brokers). Assembled by the SableWeb route from leads.json + kol_candidates JOIN |
| `EnrichmentRequest` | sidecar request body | `{handle, persona, project_context, bank_signal}` |
| `Enrichment` | sidecar response body | Structured intel: location, bio, themes, likes, dislikes, communities, notable_mutuals, top_tweets, commonality_with_operator, commentary, signal_metadata, live_data_source |
| `LiveDataSource` | inside Enrichment | Provenance: `{provider, fetched_at_utc, tweet_count, profile_present}` so the UI can display "grounded in N real tweets" |
| `EnrichmentRequest` (Zod, SableWeb) | route validation | Same shape with `.strict()` |

DB cache: `kol_enrichment` (SablePlatform migration 041) — `(candidate_id,
operator_email, operator_persona, fetched_at, payload_json, grok_model,
cost_usd)`, indexed on `(candidate_id, operator_email, fetched_at DESC)`.

---

## Failure modes the sidecar maps explicitly

| Cause | Exception | HTTP status | Detail body |
|---|---|---|---|
| Placeholder persona (ben) | `GrokPersonaPlaceholderError` | 409 | `{error: "persona_placeholder", persona}` |
| Handle doesn't resolve on X | `LiveDataHandleNotFoundError` | 404 | `{error: "handle_not_found", handle}` |
| SocialData balance depleted | `LiveDataBalanceExhaustedError` | 503 | `{error: "socialdata_balance_exhausted"}` |
| SocialData transient (network / 5xx after retries) | `LiveDataUnavailableError` | 503 | `{error: "live_data_unavailable", message}` |
| xAI auth failure | `GrokAuthError` | 503 | plain string |
| xAI returns unparseable response | `GrokParseError` | 502 | plain string |
| xAI 5xx/429 after retries | `GrokAPIError` | 502 | plain string |
| Pydantic input validation failure | (FastAPI default) | 422 | Pydantic error list |

SableWeb route surfaces 404 `handle_not_in_graph` when the handle isn't in
leads.json, kol_candidates by handle, OR the client's kol_follow_edges
(see KO-6 in AUDIT_LOG for the tiered-lookup pattern).

---

## Cost & quota

- ~$0.05 xAI Grok + ~$0.004 SocialData = **~$0.054 per refresh**.
- **10 refreshes/operator/24h** cap enforced by SableWeb's
  `checkEnrichCandidateQuota` against `kol_create_audit`.
- Cached re-reads (GET) are free; only POSTs tick the quota.
- Cost rows logged to `cost_events` as `sablekol.socialdata_enrich_profile`
  (flat $0.0002) + `sablekol.socialdata_enrich_tweets`
  (`max(1, tweet_count) * $0.0002`). xAI Grok spend is currently uninstrumented
  — a known gap; would mirror the existing Anthropic cost-logging pattern when
  added.

---

## Testing

- `tests/test_socialdata_live.py` — 16 tests covering the in-repo SocialData
  fetcher: profile + tweets, 402 / 404 / 5xx-retry paths, screen-name-vs-numeric-id
  enforcement, locked-account skip.
- `tests/test_grok_api.py` — `enrich_candidate` happy path + per-persona prompt
  injection assertions + cost-logger contract (logger fires before Grok, logger
  exception is swallowed, etc.). Tests pass canned `socialdata_fetcher` +
  `cost_logger` stubs to avoid real network / DB.
- `tests/test_preflight_service.py` — sidecar HTTP error mapping for each
  failure-mode row in the table above.
- SableWeb `tests/api-kol-enrich-candidate.test.ts` — route-level GET/POST
  coverage + tiered candidate resolution (KO-6) + persona-mirror lockstep
  against the fixture.

To smoke-test a real enrichment end-to-end on prod:

```bash
ssh root@<hetzner> 'docker exec sable-web-sable-kol-preflight-1 python -c "
from sable_kol.grok_api import enrich_candidate
from sable_kol.preflight_schemas import CandidateBankSignal
bank = CandidateBankSignal(handle=\"CahitArf11\", tier=\"A\",
    social_proximity_brokers=[], operator_confirmed_intros=[])
print(enrich_candidate(handle=\"CahitArf11\", persona=\"arf\",
    project_context=\"smoke\", bank_signal=bank).model_dump_json(indent=2)[:1500])"'
```

Cost per smoke: ~$0.054. Don't loop without intent.
