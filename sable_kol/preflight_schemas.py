"""Pydantic schemas for the SableKOL preflight sidecar service.

Per the v3 wizard plan (`docs/any_project_wizard_plan.md`), every Grok-derived
response carries a ``signal_metadata`` block so the wizard UI can label the
fields as AI-assisted with a freshness timestamp. The signal_type is always
``interpretive`` for Grok output (judgment-based, not deterministic).

These schemas are imported by both ``sable_kol.grok_api`` (the xAI client) and
``sable_kol.preflight_service`` (the FastAPI surface). They are intentionally
strict — any field Grok returns that doesn't match the schema causes a
validation failure that the worker treats as unrecoverable for that step.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from sable_kol.persona_priming import PersonaSlug


SignalSource = Literal["grok_xai_live", "operator_manual"]
SignalType = Literal["interpretive", "factual", "historical"]
PrimaryArchetype = Literal[
    "creator", "trader", "developer", "founder", "influencer", "other"
]
CredibilitySignal = Literal["high", "medium", "low", "unclear"]


class SignalMetadata(BaseModel):
    """AI-signal labeling per SableWeb AGENTS.md signal taxonomy.

    Attached to every Grok-derived response so the wizard UI can render an
    AI-assisted chip + freshness timestamp on every field traceable back here.
    """

    source: SignalSource
    model: str
    fetched_at_utc: str
    signal_type: SignalType = "interpretive"
    caveat: str | None = None


class AxisPair(BaseModel):
    """One candidate (x, y) axis pair for the network graph.

    Operators pick one of N candidates in Step 2 of the wizard. ``rationale``
    is a short explanation of why these axes fit the project; UI may show it
    on hover.
    """

    x: str
    y: str
    rationale: str | None = None


class ComparableProject(BaseModel):
    """One Grok-suggested similar-audience project.

    Used in Step 3 of the wizard. ``handle`` is the bare X handle (no @).

    ``handle_verified`` is Grok's self-reported confidence that it actually
    visited the live X profile (vs. composing a plausible-sounding handle
    from the project name). Defaults to True for back-compat with older
    prompts that didn't ask for verification, but the current
    ``_build_comparable_prompt`` in ``grok_api.py`` requires it explicitly
    after Grok hallucinated 3/6 TIG comparables on 2026-05-10
    (`bittensor_` suspended, `eleutherai` 404, `gensynnetwork` 404).
    Operators should re-validate either way before paid extraction.
    """

    handle: str
    handle_verified: bool = True
    rationale: str
    shared_themes: list[str] = Field(default_factory=list)


class EnrichedHandle(BaseModel):
    """The enrich_handle() return shape — basic profile fields + interpretive
    tags + axis candidates. Mirrors the existing ``grok_import.py`` field
    surface where they overlap, but uses the wizard's archetype enum (which
    differs from the bank-ETL enum on purpose).
    """

    twitter_id: str | None = None
    handle: str
    # Most string fields tolerate None — Grok occasionally emits null when the
    # field is unknown rather than the empty string we'd ideally get. The
    # validator coerces None → "" so downstream code (YAML write, UI render)
    # doesn't have to special-case nullability.
    bio: str = ""
    followers: int | None = None
    verified: bool = False
    is_active: bool = True
    primary_archetype: PrimaryArchetype = "other"
    primary_sectors: list[str] = Field(default_factory=list)
    credibility_signal: CredibilitySignal = "unclear"
    real_name_known: bool = False
    listed_count: int | None = None
    tweets_count: int | None = None
    following: int | None = None
    notes: str | None = None
    recent_themes: list[str] = Field(default_factory=list)
    audience_archetype: str = ""
    axis_candidates: list[AxisPair] = Field(default_factory=list)

    # Grok occasionally emits `null` for unknown string fields rather than the
    # empty string we'd ideally get. Coerce on the way in so the wire format
    # downstream (Zod, YAML write, UI) never has to special-case nullability.
    @field_validator("bio", "audience_archetype", mode="before")
    @classmethod
    def _coerce_none_to_empty(cls, v):
        return "" if v is None else v

    # Same shape for booleans Grok renders as null when unknown (e.g. `verified`
    # for accounts whose blue-check state isn't visible in the live X read).
    # Treat null as the conservative default rather than failing schema validation.
    @field_validator("verified", "is_active", "real_name_known", mode="before")
    @classmethod
    def _coerce_none_bool(cls, v, info):
        if v is None:
            # is_active defaults to True (account exists unless proven otherwise);
            # verified and real_name_known default to False (don't claim what's not visible).
            return True if info.field_name == "is_active" else False
        return v


class PreflightRequest(BaseModel):
    """Inbound payload for the sidecar /preflight endpoint.

    The three optional priming fields (``context``, ``exclude_handles``,
    ``allow_non_crypto_research``) mirror the keyword args on
    ``build_preflight_response`` so the SableWeb wizard can plumb operator
    priming through Step 1. ``context`` disambiguates thin bios;
    ``exclude_handles`` keeps other Sable-managed clients out of the
    comparable-projects pool; ``allow_non_crypto_research`` relaxes the
    "non-crypto consumer brands" exclusion for research-leaning clients.
    """

    handle: str
    context: str | None = None
    exclude_handles: list[str] | None = None
    allow_non_crypto_research: bool = False


class PreflightResponse(EnrichedHandle):
    """Bundled enrich + suggest_comparable response.

    The wizard Step 1 → Next button POSTs to ``/preflight`` and gets back
    everything needed to pre-fill steps 2 + 3 in one round-trip.
    """

    comparable_projects: list[ComparableProject] = Field(default_factory=list)
    signal_metadata: SignalMetadata


class SuggestComparableRequest(BaseModel):
    """Inbound payload for /suggest-comparable.

    Same priming-flag surface as :class:`PreflightRequest`, used when the
    operator changes themes mid-wizard and re-runs only the comparable
    suggestion pass.
    """

    handle: str
    themes: list[str] = Field(default_factory=list)
    context: str | None = None
    exclude_handles: list[str] | None = None
    allow_non_crypto_research: bool = False


class SuggestComparableResponse(BaseModel):
    source_handle: str
    comparable_projects: list[ComparableProject] = Field(default_factory=list)
    signal_metadata: SignalMetadata


class ReuseCheckRequest(BaseModel):
    handles: list[str]
    freshness_days: int = 180


class ReuseCheckResponse(BaseModel):
    """Reuse split + cost estimate for the wizard Step 3 live debounce.

    ``already_have`` and ``must_fetch`` echo the input handles (lowercased,
    @-stripped). ``estimated_cost_usd`` is a fixed-rate projection of the
    SocialData spend for fetching the ``must_fetch`` cohorts.
    """

    already_have: list[str] = Field(default_factory=list)
    must_fetch: list[str] = Field(default_factory=list)
    estimated_cost_usd: float = 0.0
    freshness_days: int = 180


# ---------------------------------------------------------------------------
# Cold-intro draft (KO-3) — per-candidate Grok-drafted opener
# ---------------------------------------------------------------------------


class CandidateIntroSignal(BaseModel):
    """Whitelisted bank signal sent to Grok for a per-candidate cold-intro draft.

    Pydantic ``extra='forbid'`` — anything the SableWeb route fails to
    strip is rejected at the API boundary instead of silently leaking to
    xAI. The list of fields here is the contract: changing it requires a
    matching change in the SableWeb assembler and the Zod mirror.
    """

    model_config = ConfigDict(extra="forbid")

    handle: str
    display_name: str | None = None
    bio_snapshot: str | None = Field(default=None, max_length=400)
    archetype: str | None = None
    sector_tags: list[str] = Field(default_factory=list)
    top_signals: list[str] = Field(default_factory=list, max_length=5)
    cluster_label: str | None = None
    tier: str | None = None


class ColdIntroRequest(BaseModel):
    """Inbound payload for the sidecar /draft-intro endpoint."""

    model_config = ConfigDict(extra="forbid")

    handle: str
    persona: PersonaSlug
    project_context: str = Field(default="", max_length=600)
    candidate_signal: CandidateIntroSignal


class ColdIntroDraft(BaseModel):
    """Outbound payload for the sidecar /draft-intro endpoint.

    ``intro_text`` is capped at 320 chars (covers the ≤280 plan target plus
    a small allowance for line breaks). ``suggested_angle`` is one short
    line of operator-facing reasoning — what bank signal this draft leans
    on — which the UI surfaces under the opener.
    """

    model_config = ConfigDict(extra="forbid")

    intro_text: str = Field(max_length=320)
    # 400 chars: Grok's "why this fits" justification routinely runs 220-280
    # chars even when the prompt asks for shorter. Cap is ceiling, not target.
    # The 200-char cap rejected useful drafts on the first live smoke (2026-05-10).
    suggested_angle: str = Field(max_length=400)
    signal_metadata: SignalMetadata
