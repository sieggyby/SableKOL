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


PersonaSlug = Literal["arf", "sparta", "alex", "ben"]


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

    twitter_handle: str = ""
    """Operator's actual X handle without the @ prefix. Distinct from
    `display_name` because the conversational name (e.g. "Arf") often
    differs from the X handle (e.g. "CahitArf11"). Grok needs the real
    handle to do live X mutual-overlap lookups: "does the target follow
    @CahitArf11" etc. Empty string is treated as 'unknown' — Grok will
    fall back to `display_name` in that case."""

    real_name: str | None = None
    """Real legal name, if operator OK with it being sent to xAI for
    commonality matching ("did the target tweet mentioning operator's real
    name?"). Null for operators who prefer pseudonymity even toward Grok."""

    location: str = ""
    """Geo. Free text — "NYC", "Lagos / Berlin axis", "PNW". Used both for
    geographic commonality (target is also in city X) and for cultural
    inference (PNW-coded posting register, etc.)."""

    bio: str = ""
    """Public-ish bio. ≤800 chars. What the operator tells the world they
    are. Drives Grok's read on what topics + register the operator can
    plausibly engage with. (Originally capped at 300 chars; bumped to 800
    on 2026-05-10 when filling out Arf's profile — the richness was
    paying for itself in commonality output and the prompt token budget
    is comfortable.)"""

    themes: list[str] = field(default_factory=list)
    """Up to 10 short tags describing what the operator posts about. Drives
    theme-overlap detection with target.recent_themes. e.g.
    ["crypto", "stocks", "tech", "memes", "AI / LLM research", "politics"]."""

    likes: list[str] = field(default_factory=list)
    """Up to 6 tight phrases describing what resonates with the operator
    when they see it in someone else's TL. e.g. ["indie music", "philosophy",
    "sci-fi"]. Operator-side of the likes-overlap commonality field."""

    dislikes: list[str] = field(default_factory=list)
    """Up to 4 tight phrases. e.g. ["VC-coded threads", "AI slop",
    "credential-flexing"]. Used in the "do not pitch as if X"
    inverse-commonality signal."""

    communities: list[str] = field(default_factory=list)
    """Up to 10 named communities the operator participates in. e.g.
    ["FWB", "Multisynq", "Solana staking ecosystem", "Welfare Warriors discord"].
    Drives mutual-community detection."""

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
# Filled out 2026-05-10 via grill-me round with Sieggy. Arf is anonymous
# (real_name null intentionally). The bio captures the most load-bearing
# nuance: history major + MBA but allergic to credentialism — Grok needs
# to know not to lean on either when computing commonality. Mutual list
# is the actively-engaged set, not the comprehensive follow graph.
ARF = PersonaPriming(
    display_name="Arf",
    twitter_handle="CahitArf11",
    real_name=None,
    location="NYC/Istanbul/Miami",
    bio=(
        "Sable operator, former community manager in crypto for Solana "
        "projects and Monad projects. Generally interested in "
        "stocks/crypto/technology, politics are center left and tends "
        "to like music/sports/movies that are a little off the beaten "
        "path. Dark sense of humor. Arf might say he's a storyteller, "
        "but he'd never tweet that. He's a serious person but never "
        "takes himself seriously on the TL. History major and MBA "
        "holder, though credentialism is antithetical to Arf's essence."
    ),
    themes=[
        "crypto",
        "stocks",
        "tech",
        "sports",
        "music",
        "off-beat film",
        "memes",
        "monad memes",
        "AI / LLM research",
        "politics",
    ],
    likes=[
        "indie music",
        "house music",
        "jazz music",
        "philosophy",
        "sci-fi",
    ],
    dislikes=[
        "VC-coded threads",
        "credential-flexing",
        "AI slop",
        "prejudicial language / bigotry",
    ],
    communities=[
        "FWB",
        "Multisynq",
        "Solana staking ecosystem",
        "Welfare Warriors discord",
        "Monad ecosystem OG",
        "Arbitrum",
        "Ethereum",
        "Kamigotchi",
    ],
    notable_mutuals=[
        "p0isonxs",
        "0xWoah",
        "monasex_1",
        "billmondays",
        "0xDaes",
    ],
    values=[
        "anti-credentialism",
        "anti-grift",
        "intellectually serious without ego",
        "off-beat over mainstream",
    ],
    voice_signature=(
        "warm crypto-native peer-level register, dark humor under the "
        "surface, lowercase-leaning. observations land before the ask. "
        "allergic to corporate phrasing or anything that smells like a "
        "SaaS template."
    ),
)


# --- SPARTA ---
# Filled out 2026-05-10 (v2) from a SocialData read of his actual X
# timeline (30 recent tweets + canonical profile). Earlier placeholder
# had to guess; the real account turned out far more distinctive than
# inferred. He's TIG-leadership-adjacent (hosts AMAs with
# @Dr_JohnFletcher, uses "we/our" framing about the protocol), heavy
# thesis-poster register, transhuman/gnostic vocabulary, references
# Squaresoft / Parasite Eve / Evangelion's Magi fluidly. The display
# "Sparta (𝔦, 𝔦)" is the imaginary-unit pair — a math/transhuman signal.
SPARTA = PersonaPriming(
    display_name="Sparta",
    twitter_handle="0x_Asuka",
    real_name=None,
    location="",
    bio=(
        "Web3 researcher since 2017, VC since 2020 (verbatim X bio: "
        "'Transhumanist Gnostic. Mana-Sama fan account.'). Co-runs ARKN "
        "and Sable; known internally as 'Sparta', anon online as 'Asuka'. "
        "Deeply embedded in TIG — hosts community AMAs with "
        "@Dr_JohnFletcher, frames himself with 'we/our' about the "
        "protocol. Heavy thesis-poster: $TIG-as-next-$TAO assertions "
        "mixed with anime + retro-gaming + religious-adjacent side-quests. "
        "Maintains a TIG ARG side project at 329ga8dh4x.com."
    ),
    themes=[
        "TIG ecosystem",
        "algorithmic innovation vs hardware scaling",
        "AGI / superintelligence",
        "transhumanism / gnostic theology",
        "Squaresoft / PS1-era games",
        "anime (Evangelion-coded)",
        "web3 research",
        "decentralized AI",
    ],
    likes=[
        "Mana-Sama (Malice Mizer / Moi dix Mois)",
        "Parasite Eve / Squaresoft retro",
        "Evangelion's Magi as multi-agent superintelligence",
        "Karpathy on open-source AI",
        "Bryan Johnson Don't Die orbit",
        "math-coded handles + sigils",
    ],
    dislikes=[
        "speculative capital that ignores algorithmic innovation",
        "hardware-only AI scaling narratives",
        "shallow $TIG dismissal",
    ],
    communities=[
        "TIG core (leadership-adjacent)",
        "Sable", "ARKN",
        "Mana-Sama fan circuit",
        "transhumanist / gnostic X niches",
        "retro-gaming + anime mutuals",
    ],
    notable_mutuals=[
        "tigfoundation", "Dr_JohnFletcher", "CreateTheDots",
        "CahitArf11", "siegby", "karpathy", "HEAVYWASH_",
    ],
    values=[
        "transhumanist / gnostic-religious frame",
        "decentralized open algorithmic innovation",
        "anti-shallow-speculation",
        "aesthetic + intellectual coherence over polish",
    ],
    voice_signature=(
        "assertive thesis-poster — 'you heard it here first' confidence. "
        "Mixes high-conviction TIG evangelism with anime, religious, "
        "transhuman side-quests fluidly. Cites Karpathy, Bryan Johnson, "
        "Evangelion, Squaresoft in the same breath. Math-sigil register "
        "in bio + handle."
    ),
)


# --- ALEX ---
# Filled out 2026-05-10 (v2) from a SocialData read of his actual X
# timeline. Earlier Grok-inferred profile was generic; real timeline
# revealed he's a TIG amplifier in pure form — ~80% of his recent
# 20 tweets are @tigfoundation retweets. When he posts originally, it's
# TIG-thesis hype ("world-shifting implications") or longevity-adjacent
# pivots (Don't Die / Bryan Johnson reply about DD + TIG as
# 'natural bedfellows'). Display "Ale𝕏" uses the mathematical italic X.
ALEX = PersonaPriming(
    display_name="Alex",
    twitter_handle="CreateTheDots",
    real_name="Alex Malone",
    location="San Francisco, CA",
    bio=(
        "Alex Malone. ARKN co-builder, Sable operator. Verbatim X bio: "
        "'Collaborations in Science & Tech.' Verified, 3.9K followers, "
        "10K+ lifetime tweets. Heavy TIG amplifier — most recent timeline "
        "is @tigfoundation retweets. When he posts originally, frames "
        "TIG as 'world-shifting' for humanity's future. Longevity-curious "
        "(replied to Bryan Johnson framing Don't Die + TIG as 'natural "
        "bedfellows'). Boost-mode rather than thesis-mode poster."
    ),
    themes=[
        "TIG ecosystem",
        "algorithmic innovation",
        "longevity / Don't Die",
        "open-innovation evangelism",
        "biotech adjacent",
        "blockchain on Base",
        "science + tech collaborations",
    ],
    likes=[
        "TIG protocol momentum",
        "Bryan Johnson Don't Die orbit",
        "Cudis Wellness / wearables",
        "ambitious research framings",
        "long-game open-innovation moves",
    ],
    dislikes=[],  # Sparse — Alex is amplifier-mode; dislikes don't surface in retweets
    communities=[
        "TIG (community member, not core)",
        "ARKN", "Sable",
        "New Paradigm Institute orbit",
        "Don't Die / biotech longevity",
        "Lab Tokens / Cudis Wellness",
    ],
    notable_mutuals=[
        "tigfoundation", "0x_Asuka", "Dr_JohnFletcher", "CahitArf11",
        "siegby", "bryan_johnson", "NewParadigmInst", "heavyweight",
        "labtokensol",
    ],
    values=[
        "open-innovation evangelism",
        "longevity-curious",
        "amplify-the-signal posting ethos",
    ],
    voice_signature=(
        "amplifier register: mostly retweets earnest aligned voices. "
        "When he writes originally, leans 'world-shifting implications' "
        "and 'humanity's future' framing. Sincere, hype-adjacent without "
        "shilling. Glyph play on his own handle (Ale𝕏 with math italic)."
    ),
)


# --- BEN ---
BEN = PersonaPriming(
    display_name="ben",
    twitter_handle="",
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
    "alex": ALEX,
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
    # Render header with X handle when known so Grok can do mutual-overlap
    # lookups against the operator's actual on-platform identity. Fall back
    # to display_name when twitter_handle is empty (e.g. unfilled profile).
    header_handle = f"@{p.twitter_handle}" if p.twitter_handle else f"@{p.display_name}"
    lines: list[str] = [
        f"OPERATOR PROFILE — {p.display_name} ({header_handle} on X)",
        f"- display_name: {p.display_name}",
    ]
    if p.twitter_handle:
        lines.append(f"- twitter_handle: @{p.twitter_handle}")
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
