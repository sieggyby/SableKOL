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

from pydantic import BaseModel, Field


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
    """

    handle: str
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


class PreflightRequest(BaseModel):
    handle: str


class PreflightResponse(EnrichedHandle):
    """Bundled enrich + suggest_comparable response.

    The wizard Step 1 → Next button POSTs to ``/preflight`` and gets back
    everything needed to pre-fill steps 2 + 3 in one round-trip.
    """

    comparable_projects: list[ComparableProject] = Field(default_factory=list)
    signal_metadata: SignalMetadata


class SuggestComparableRequest(BaseModel):
    handle: str
    themes: list[str] = Field(default_factory=list)


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
