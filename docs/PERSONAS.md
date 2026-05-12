# Operator personas

The per-candidate enrichment (KO-3 v2.5) computes commonality between the
operator running the lookup and the candidate they're researching. To do that,
Grok needs to know *who the operator is* — their voice, communities, mutuals,
themes, values. That's the operator persona.

`sable_kol/persona_priming.py` is the canonical source. SableWeb mirrors via the
`sable-kol persona-manifest --json` CLI; the fixture at
`SableWeb/tests/fixtures/persona_manifest.json` locksteps the TS persona enum.
Adding or removing a slug requires touching both sides.

---

## Current personas

| Slug | Email | Twitter | State |
|---|---|---|---|
| `arf` | `arfcahit1910@gmail.com` | `@CahitArf11` | full priming (grill-me'd 2026-05-10) |
| `sparta` | `george@arkn.io` | `@0x_Asuka` | partial — bio + handle + grounded themes from SocialData read |
| `alex` | `alex@arkn.io` | `@CreateTheDots` | partial — same as Sparta |
| `ben` | `ben@arkn.io` | (none) | **placeholder** — enrichment route 409s for him until filled |

Sieggy (`siegby@gmail.com`) is on `KOL_CREATE_EMAILS` for project creation but
is intentionally absent from `KOL_CREATE_EMAIL_TO_PERSONA` — he doesn't run
outreach, so the enrichment block is hidden on his login.

---

## PersonaPriming schema

The dataclass in `sable_kol/persona_priming.py`:

| Field | Use in prompt | Notes |
|---|---|---|
| `display_name` | "operator @<display_name>" framing line | Conversational name |
| `twitter_handle` | "(@<handle> on X)" + mutual-overlap detection | Bare X handle, no `@`. Distinct from `display_name` because slugs/display rarely match X handles (Arf's display is "Arf" but X is `@CahitArf11`) |
| `real_name` | mentioned in prompt only if non-null | Anon operators leave null; flag if OK to send to xAI |
| `location` | geographic-overlap commonality | Free text — "NYC", "PNW", or null |
| `bio` | ≤800 chars — what operator tells the world they are | Drives Grok's read on what topics + register the operator can plausibly engage with |
| `themes` | ≤10 — what operator posts about | Matched against `target.recent_themes` for overlap |
| `likes` | ≤6 — what resonates with operator in others' TLs | Operator-side of likes-overlap commonality |
| `dislikes` | ≤4 — what makes operator bounce | Inverse-commonality signal ("don't pitch as if X") |
| `communities` | ≤10 — named communities operator participates in | Drives mutual-community detection (FWB, ARKN, Sable, named Discord servers, etc.) |
| `notable_mutuals` | ≤10 — bare X handles operator engages with regularly | Critical for "you both follow @X" output. Be specific — actual mutuals, not "everyone they follow" |
| `values` | ≤4 — aesthetic / ethical commitments | "open-source bias", "anti-grift", etc. |
| `voice_signature` | ≤200 chars — how operator sounds in DMs | Grok uses sparingly; not to write IN that voice but to know what targets respond well to it |
| `placeholder` | True → enrichment route 409s | Only ben today |

All fields are bounded so a poorly-edited profile can't blow the prompt budget.
The Python dataclass has an import-time `_validate_persona_table()` check that
guards against drift between `PersonaSlug` Literal and `PERSONAS` dict keys.

---

## How to fill a persona (the grill-me protocol)

Empty profiles cause Grok to fabricate commonality from thin air. A good
profile takes ~30 min via a one-field-at-a-time interview with the operator.
Sieggy's Arf fill (2026-05-10) is the reference example — see commit `d9e2f65`
for the actual content + `AUDIT_LOG.md` for the field list and rationale.

Pattern:

1. Recommend a default for each field based on what you can infer (operator
   email, X bio if visible via SocialData, what other personas already have).
2. Operator confirms, edits, or pushes back. Skip-able fields use `None` (e.g.
   `real_name` for anon operators).
3. After all 12 fields: regenerate the SableWeb fixture
   (`.venv/bin/python -m sable_kol.cli persona-manifest --json >
   ../SableWeb/tests/fixtures/persona_manifest.json`), run tests both sides.
4. Commit + deploy (sidecar rebuild for the Python file; SableWeb tests-only
   if just the fixture changed).

**What makes a good profile** (vs the conservative stubs Sparta + Alex have
today): non-empty `notable_mutuals` extracted from actual X interactions, not
inferred; communities named (FWB, specific Discords) rather than generic
("crypto", "tech"); voice_signature grounded in observable register from the
operator's actual posts rather than aspirational framing.

---

## Discovering operator profile content from real X data

For an operator you don't know well personally, the SocialData live fetcher can
seed a draft profile:

```bash
SOCIALDATA_API_KEY=... .venv/bin/python -c "
from sable_kol.socialdata_live import fetch_live_signal
s = fetch_live_signal('CahitArf11', tweet_count=30)
print('bio:', s.profile.bio)
print('location:', s.profile.location)
for t in s.tweets[:10]:
    print(f'[{t.type}]', t.text[:200])
"
```

That gives you raw material. Then run the grill-me with the operator to confirm
the conversational nuances (likes/dislikes/voice_signature) the timeline doesn't
fully surface. Don't ship Grok-inferred profile fields as canonical without
operator confirmation — that pattern produced the early Alex profile with
hallucinated mutuals (`techinnovators` / `creativeminds`) before I learned
better.

---

## Adding a new operator

End-to-end checklist:

1. Add email to `SableWeb/src/lib/allowlist.ts` (`ALLOWLIST_JSON` in prod env)
   if they aren't already on the general ops allowlist.
2. Add email to `KOL_CREATE_EMAILS` set in
   `SableWeb/src/lib/kol-create-allowlist.ts` if they should be able to create
   new KOL projects (the wizard surface).
3. Add `email → persona_slug` to `KOL_CREATE_EMAIL_TO_PERSONA` map in the same
   file.
4. Add the slug to `PersonaSlug` Literal in
   `sable_kol/persona_priming.py` and add the corresponding `PERSONAS[<slug>]
   = PersonaPriming(...)` entry. Start as `placeholder=True` if you don't have
   the profile content yet; the enrichment route will 409 until you flip it.
5. Add the slug to `PersonaSlugSchema` Zod enum in
   `SableWeb/src/lib/kol-create-schemas.ts`.
6. Update `KOLNetwork.tsx` + `KOLTagPanel.tsx` `personaSlug` prop type union.
7. Regenerate the fixture: `sable-kol persona-manifest --json > SableWeb/tests/fixtures/persona_manifest.json`.
8. Run the persona-mirror lockstep test on both sides; both must pass.
9. Commit, push, redeploy (sidecar rebuild + web rebuild).

---

## Removing an operator

Mirror of the above:

1. Remove from `PersonaSlug` Literal + `PERSONAS` dict + email map.
2. Remove from Zod enum + prop unions in SableWeb.
3. Regenerate fixture.
4. **Keep** any existing rows in `kol_enrichment` keyed on their email —
   historical record, no need to delete.
5. Existing audit-log rows referencing their email also stay.

This is what happened to Sieggy in v2 (commit `db39b9b`). His email stayed on
`KOL_CREATE_EMAILS` for project creation but `operatorPersonaForEmail` now
returns `null` so the enrichment panel is hidden on his login.
