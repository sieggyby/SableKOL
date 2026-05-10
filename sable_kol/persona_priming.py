"""Operator persona profiles for the per-candidate Grok enrichment.

Each Sable operator who runs outreach has a distinct online identity —
where they live, what communities they're part of, what they post about,
who their notable mutuals are. Grok uses this profile in the
enrichment prompt to compute *commonality* between operator and target:
"you both posted about typewriter aesthetics in the same week"; "you both
follow @x and they retweeted you Tuesday." That's the load-bearing
signal the operator uses to write a non-cringe cold intro.

This module is the **canonical source of truth** for operator personas.
The TS persona union in SableWeb's `kol-create-schemas.ts` mirrors this
via the `sable-kol persona-manifest --json` CLI verb. Tests on both
sides of the cross-repo boundary lockstep against the same fixture.

History: KO-3 v1 (2026-05-10 morning) shipped these as voice-register
priming for Grok to *write the DM*. That output was uniformly cringe.
The redesign (KO-3 v2, this file): operator profile feeds commonality
computation, NOT prose generation. Grok produces intel; operator
authors. `sieggy` was removed in v2 — Sieggy doesn't run outreach
himself.

`ben` is a placeholder slug. Operator allowlist accepts him today but
no priming text exists yet. Until the operator supplies a real profile
and flips `placeholder=False`, the sidecar and SableWeb route both
409-reject any enrichment request resolved to ben. The block is
deliberately not gated by "fill it out in the UI" — that would itself
be a persona-tuning UI, which is out of scope.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, get_args


PersonaSlug = Literal["arf", "sparta", "ben"]


@dataclass(slots=True, frozen=True)
class PersonaPriming:
    """Operator profile injected into the Grok enrichment prompt.

    Used by Grok to compute commonality between operator and target.
    All free-text fields are bounded so a poorly-edited profile can't
    blow the prompt budget. Real-priming entries are tuned iteratively
    from operator feedback; the placeholder entry is a hard 409-block
    until populated.
    """

    display_name: str
    """How the operator likes to be referred to in the prompt — could be a
    handle, a real first name, or a nickname. Used in the system prompt's
    framing line ("you are drafting intel for operator @arf to read")."""

    real_name: str | None
    """Real legal name, if operator OK with it being sent to xAI for
    commonality matching ("did the target tweet mentioning operator's real
    name?"). Null for operators who prefer pseudonymity even toward Grok."""

    location: str
    """Geo. Free text — "NYC", "Lagos / Berlin axis", "PNW". Used both for
    geographic commonality (target is also in city X) and for cultural
    inference (PNW-coded posting register, etc.)."""

    bio: str
    """Public-ish bio. ≤300 chars. What the operator tells the world they
    are. Drives Grok's read on what topics + register the operator can
    plausibly engage with."""

    themes: list[str] = field(default_factory=list)
    """Up to 6 short tags describing what the operator posts about. Drives
    theme-overlap detection with target.recent_themes. e.g.
    ["NFTs", "fashion-crypto", "FWB-era discourse", "DAO governance"]."""

    likes: list[str] = field(default_factory=list)
    """Up to 6 tight phrases describing what the operator likes / aligns
    with. e.g. ["typewriter aesthetics", "low-ego curators", "open-source
    AI bias"]. Operator-side of the likes-overlap commonality field."""

    dislikes: list[str] = field(default_factory=list)
    """Up to 4 tight phrases. e.g. ["VC-coded threads", "AI-PFP
    accounts"]. Used in the "do not pitch as if X" inverse-commonality
    signal."""

    communities: list[str] = field(default_factory=list)
    """Up to 6 named communities the operator participates in. e.g.
    ["FWB", "ARKN", "Sable", "Friends With Benefits cohort", "specific
    Discord servers"]. Drives mutual-community detection."""

    notable_mutuals: list[str] = field(default_factory=list)
    """Up to 10 bare X handles (no @) the operator interacts with regularly.
    Used by Grok to look for shared connections: "you both follow @X and
    they engaged with both your accounts in the last month."""

    values: list[str] = field(default_factory=list)
    """Up to 4 short phrases describing the operator's aesthetic /
    ethical commitments. e.g. ["open-source bias", "anti-VC-coded register",
    "low-ego curation"]. Drives values-overlap commonality."""

    voice_signature: str = ""
    """≤200 chars. One line capturing how the operator sounds in DMs —
    register, sentence shape, what's recognizably theirs. Used by Grok
    sparingly: not to write IN that voice, but to know "this kind of
    target would respond well to this kind of voice."""

    placeholder: bool = False
    """True for slugs the allowlist accepts but for which priming is not
    yet authored. The sidecar + SableWeb route both 409-reject these."""


# --- ARF ---
# TODO: Sieggy is providing real values via the grill-me round. Until
# they land, the priming is intentionally sparse — Grok gets just
# enough signal to differentiate Arf from Sparta in the prompt without
# fabricating commonality from thin air. Update via PR when filled.
ARF = PersonaPriming(
    display_name="arf",
    real_name=None,
    location="<TBD>",
    bio="Sable operator. Crypto-native cultural curator. <TBD: fill in via PR>",
    themes=["crypto culture", "fashion-crypto"],
    likes=["low-ego curators"],
    dislikes=["VC-coded threads"],
    communities=["Sable", "ARKN"],
    notable_mutuals=[],
    values=["open-source bias"],
    voice_signature=(
        "warm crypto-native, peer-level observation before the pitch, "
        "lowercase-leaning, allergic to corporate phrasing"
    ),
)


# --- SPARTA ---
# TODO: Sieggy is providing real values via the grill-me round.
SPARTA = PersonaPriming(
    display_name="sparta",
    real_name=None,
    location="<TBD>",
    bio="Sable operator. Founder-to-founder communicator. <TBD: fill in via PR>",
    themes=["crypto founder discourse", "ops"],
    likes=["plain language"],
    dislikes=["irony posting"],
    communities=["Sable", "ARKN"],
    notable_mutuals=[],
    values=["business-first"],
    voice_signature=(
        "direct, plain-spoken, founder-to-founder. names the project, "
        "names the ask, references one concrete reason — in that order"
    ),
)


# --- BEN ---
BEN = PersonaPriming(
    display_name="ben",
    real_name=None,
    location="<placeholder>",
    bio="<placeholder — operator must supply>",
    themes=[],
    likes=[],
    dislikes=[],
    communities=[],
    notable_mutuals=[],
    values=[],
    voice_signature="<placeholder>",
    placeholder=True,
)


PERSONAS: dict[PersonaSlug, PersonaPriming] = {
    "arf": ARF,
    "sparta": SPARTA,
    "ben": BEN,
}


def _validate_persona_table() -> None:
    """Sanity-check at import time that PERSONAS matches the PersonaSlug Literal.

    A drift here is a developer error (someone added a slug to the Literal
    but forgot the priming, or vice versa). Catching it on import is far
    less surprising than the schema-validation failure that'd otherwise
    surface mid-request.
    """
    literal_slugs = set(get_args(PersonaSlug))
    table_slugs = set(PERSONAS.keys())
    if literal_slugs != table_slugs:
        missing = literal_slugs - table_slugs
        extra = table_slugs - literal_slugs
        raise RuntimeError(
            f"persona_priming drift: missing in PERSONAS={missing!r}, "
            f"extra in PERSONAS={extra!r}"
        )


_validate_persona_table()


def manifest() -> dict[str, list[str]]:
    """Return the persona manifest as a JSON-serializable dict.

    Used by both the test suite (`tests/test_persona_priming.py`) and the
    `sable-kol persona-manifest --json` CLI. SableWeb's CI reads the
    fixture this CLI emits to lockstep its TypeScript persona union.
    """
    return {
        "slugs": sorted(PERSONAS.keys()),
        "placeholder_slugs": sorted(
            slug for slug, p in PERSONAS.items() if p.placeholder
        ),
    }


def is_placeholder(slug: PersonaSlug) -> bool:
    """Convenience for callers that want to short-circuit before the sidecar."""
    return PERSONAS[slug].placeholder


def priming_for(slug: PersonaSlug) -> PersonaPriming:
    """Look up priming for a slug. Caller is expected to have validated the slug."""
    return PERSONAS[slug]


def operator_profile_block(slug: PersonaSlug) -> str:
    """Render the operator profile as a Markdown block for the Grok prompt.

    Used by `_build_enrich_prompt` so the same operator profile shape
    that backs commonality computation is also what gets sent to xAI —
    no hidden fields, no shape drift. Returns a string; caller embeds.
    """
    p = priming_for(slug)
    lines: list[str] = [
        f"OPERATOR PROFILE — @{p.display_name}",
        f"- display_name: {p.display_name}",
    ]
    if p.real_name:
        lines.append(f"- real_name: {p.real_name}")
    if p.location:
        lines.append(f"- location: {p.location}")
    if p.bio:
        lines.append(f"- bio: {p.bio}")
    if p.themes:
        lines.append(f"- themes: {', '.join(p.themes)}")
    if p.likes:
        lines.append(f"- likes: {', '.join(p.likes)}")
    if p.dislikes:
        lines.append(f"- dislikes: {', '.join(p.dislikes)}")
    if p.communities:
        lines.append(f"- communities: {', '.join(p.communities)}")
    if p.notable_mutuals:
        lines.append(f"- notable_mutuals: {', '.join('@' + h for h in p.notable_mutuals)}")
    if p.values:
        lines.append(f"- values: {', '.join(p.values)}")
    if p.voice_signature:
        lines.append(f"- voice_signature: {p.voice_signature}")
    return "\n".join(lines)


__all__ = [
    "PERSONAS",
    "PersonaPriming",
    "PersonaSlug",
    "is_placeholder",
    "manifest",
    "operator_profile_block",
    "priming_for",
]
