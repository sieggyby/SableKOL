"""xAI Grok client — the only module that talks to api.x.ai.

Two public functions:

* :func:`enrich_handle` — looks up a single X handle and returns an
  :class:`EnrichedHandle` (basic profile fields + interpretive tags + 2-3
  candidate axis pairs).
* :func:`suggest_comparable_projects` — returns 8-10 similar-audience
  projects on X for a given handle + themes.

Both use ``grok-4-latest`` via xAI's OpenAI-compatible chat completions
endpoint with JSON-object response format. The model has live X search
baked in.

Auth: reads ``XAI_API_KEY`` from env. Hard-fails on missing key — there is
no fallback. The sidecar's ``Dockerfile.preflight`` is the only deployment
target where this key should be set; SableWeb's bundle never sees it.

Failure handling per plan:

* 5xx: one retry with 2s backoff, then raises :class:`GrokAPIError`.
* 429: exponential backoff up to 3 attempts, then raises.
* 401/403: raises :class:`GrokAuthError` (mapped by the sidecar to HTTP 503).
* JSON parse / Pydantic validation failure: raises :class:`GrokParseError`
  — operator falls back to manual entry in the wizard.

The model name is ``grok-4-latest`` per Sieggy's call: pin to -latest, fix
breakage when it happens. If this proves unstable we pin to a date-stamped
snapshot.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

import httpx
from pydantic import ValidationError

from sable_kol.preflight_schemas import (
    AxisPair,
    ComparableProject,
    EnrichedHandle,
    PreflightResponse,
    SignalMetadata,
    SuggestComparableResponse,
)


logger = logging.getLogger(__name__)


XAI_API_URL = "https://api.x.ai/v1/chat/completions"
# 2026-05-09: bumped from grok-2-latest (deprecated/removed by xAI — returns
# "Model not found") to grok-4-latest. Sieggy's pin policy: stay on -latest,
# fix breakage when it happens. The chat/completions endpoint + JSON-object
# response format still apply unchanged.
GROK_MODEL = "grok-4-latest"

FIXED_AXIS_LIBRARY = [
    "fashion",
    "luxury",
    "streetwear",
    "technical-credibility",
    "crypto-native",
    "degen-coded",
    "cultural-relevance",
    "consumer-mainstream",
    "on-chain",
    "defi-native",
    # Research / AI / DeSci axes — added so research-leaning clients (e.g. TIG)
    # don't get force-fit into the original fashion/web3-biased library.
    "research-academic",
    "ai-ml",
    "desci-science",
    "algorithmic-quant",
    "e-acc-frontier",
    # DeAI / bounty-IP axes — added 2026-05-09 from Grok meta-audit on TIG.
    # `deai-frontier` captures the explicit Decentralized-AI positioning that
    # `e-acc-frontier` and `ai-ml` don't separate cleanly. `bounty-ip-commercialization`
    # captures the unique TIG flywheel (PoW submissions → community-voted IP →
    # licensable patents → revenue) that no other axis covers.
    "deai-frontier",
    "bounty-ip-commercialization",
]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class GrokAPIError(RuntimeError):
    """Generic xAI request failure (5xx after retries, 429 after backoff)."""


class GrokAuthError(GrokAPIError):
    """xAI rejected our API key (401 / 403)."""


class GrokParseError(GrokAPIError):
    """Response body could not be parsed as the expected schema."""


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


def _normalize(handle: str) -> str:
    h = handle.strip().lower()
    if h.startswith("@"):
        h = h[1:]
    return h


def _build_enrich_prompt(handle: str, context: str | None = None) -> str:
    axis_list = ", ".join(FIXED_AXIS_LIBRARY)
    context_block = (
        f"\nCONTEXT (operator-supplied — use to disambiguate when the public bio is thin):\n{context.strip()}\n"
        if context else ""
    )
    return f"""You have live read access to X (Twitter). Look up @{handle} on X right now and return a JSON object describing the account. This will pre-fill a KOL outreach wizard, so be accurate and concise.{context_block}

OUTPUT RULES:
- Return ONLY a JSON object. No prose, no markdown fences.
- If a field can't be determined, use null (NOT "unknown" or empty string).
- If the account is suspended, deleted, or unfindable, set is_active=false and leave other fields null where appropriate.
- Booleans are true/false (lowercase). Counts are integers.
- bio is the canonical X bio text, capped at 280 characters.
- notes is a one-line context (max 100 chars), e.g. "Cofounder of Arbitrum", "NYC fashion-tech curator". Null if nothing notable.

OBJECT SHAPE:

{{
  "twitter_id": "<string, numeric X user ID, or null>",
  "handle": "{handle}",
  "bio": "<string, up to 280 chars>",
  "followers": <int or null>,
  "verified": <bool>,
  "is_active": <bool>,
  "primary_archetype": "<one of: creator, trader, developer, founder, influencer, other>",
  "primary_sectors": ["<sector strings, 1-3 entries>"],
  "credibility_signal": "<one of: high, medium, low, unclear>",
  "real_name_known": <bool, true if posted under their real name>,
  "listed_count": <int or null>,
  "tweets_count": <int or null>,
  "following": <int or null>,
  "notes": "<one-line context, max 100 chars, or null>",
  "recent_themes": ["theme1", "theme2", "theme3"],
  "audience_archetype": "<one-line description of who follows this account>",
  "axis_candidates": [
    {{"x": "<axis label>", "y": "<axis label>", "rationale": "<brief why these axes>"}}
  ]
}}

axis_candidates: up to 3 candidate (x, y) pairs from this fixed library: {axis_list}. Pick pairs that meaningfully separate this project's audience. Return an empty array if no library pair fits well — do NOT force a bad fit.

recent_themes: 3 to 5 short keyword tags describing what the account currently posts about. Use 3 only if 4+ would be redundant; otherwise prefer 4-5 for richer downstream matching.

audience_archetype: who follows this account, one line. e.g. "fashion-leaning crypto natives", "DeFi quants and infrastructure devs".

Output the JSON object only.
"""


def _build_comparable_prompt(
    handle: str,
    themes: list[str],
    *,
    context: str | None = None,
    exclude_handles: list[str] | None = None,
    allow_non_crypto_research: bool = False,
    inclusion_hint: str | None = None,
    extra_exclusions: list[str] | None = None,
) -> str:
    theme_str = ", ".join(themes) if themes else "(unspecified)"
    context_block = (
        f"\nCONTEXT (operator-supplied):\n{context.strip()}\n"
        if context else ""
    )
    extra_excludes = ""
    if exclude_handles:
        normalized = [f"@{h.lstrip('@').strip()}" for h in exclude_handles if h.strip()]
        if normalized:
            extra_excludes = (
                f"\n- Do NOT suggest any of these handles "
                f"(operator-managed conflicts): {', '.join(normalized)}."
            )
    if extra_exclusions:
        for rule in extra_exclusions:
            r = rule.strip()
            if r:
                extra_excludes += f"\n- {r}"
    consumer_brand_rule = (
        "- Do NOT suggest celebrity accounts or non-crypto consumer brands, "
        "**unless** the account is a research lab, academic group, or AI/ML "
        "community whose audience plausibly overlaps with this project's."
        if allow_non_crypto_research
        else "- Do NOT suggest celebrity accounts or non-crypto consumer brands."
    )
    inclusion_block = (
        f"\nINCLUSION HINTS (operator-supplied — bias toward matches that fit these cues):\n- {inclusion_hint.strip()}\n"
        if inclusion_hint else ""
    )
    return f"""You have live read access to X (Twitter). I'm building a KOL outreach plan for the project @{handle}. Their themes are: {theme_str}.{context_block}

Suggest 8-10 comparable projects on X — ones whose audience overlaps meaningfully with @{handle}'s, so a follower of one would plausibly be interested in the other. Comparable means: similar themes, similar cultural register, similar audience demographics. Prefer ADJACENT communities (whose thought leaders could be converted to this project's orbit) over DIRECT competitors (whose KOLs are already locked in to the rival).{inclusion_block}

EXCLUSIONS:
- Do NOT suggest large org accounts (exchanges, big media outlets, central foundations).
{consumer_brand_rule}
- Do NOT suggest @{handle} itself.{extra_excludes}

OUTPUT RULES:
- Return ONLY a JSON object. No prose, no markdown fences.

OBJECT SHAPE:

{{
  "comparable_projects": [
    {{
      "handle": "<bare X handle, no @>",
      "rationale": "<one line, max 120 chars, why this is comparable>",
      "shared_themes": ["<theme1>", "<theme2>"]
    }}
  ]
}}

Output the JSON object only.
"""


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------


def _resolve_api_key() -> str:
    key = os.environ.get("XAI_API_KEY")
    if not key:
        raise GrokAuthError(
            "XAI_API_KEY is not set. The preflight sidecar requires this env "
            "var. Set it via the compose environment or refuse to start."
        )
    return key


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _post_chat(
    *,
    prompt: str,
    client: httpx.Client | None,
    api_key: str,
    timeout: float,
) -> dict[str, Any]:
    """POST a single-turn chat completion. Returns the parsed JSON object the
    model emitted in ``choices[0].message.content``.

    Retries:
      * 5xx: 1 retry with 2s backoff
      * 429: 3 attempts with 1s, 2s, 4s backoff
    """
    body = {
        "model": GROK_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"},
        "temperature": 0.0,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    owns_client = client is None
    if owns_client:
        client = httpx.Client(timeout=timeout)

    last_err: Exception | None = None
    try:
        for attempt in range(3):
            try:
                resp = client.post(XAI_API_URL, json=body, headers=headers)
            except httpx.HTTPError as e:
                last_err = e
                logger.warning("xAI transport error (attempt %d): %s", attempt + 1, e)
                time.sleep(2 ** attempt)
                continue

            if resp.status_code in (401, 403):
                raise GrokAuthError(
                    f"xAI auth failure ({resp.status_code}): {resp.text[:200]}"
                )
            if resp.status_code == 429:
                last_err = GrokAPIError(f"xAI 429 (attempt {attempt + 1})")
                logger.warning("xAI 429 throttling (attempt %d)", attempt + 1)
                time.sleep(2 ** attempt)
                continue
            if resp.status_code >= 500:
                last_err = GrokAPIError(
                    f"xAI {resp.status_code}: {resp.text[:200]}"
                )
                if attempt == 0:
                    time.sleep(2)
                    continue
                raise last_err
            if resp.status_code >= 400:
                raise GrokAPIError(
                    f"xAI {resp.status_code}: {resp.text[:200]}"
                )

            payload = resp.json()
            try:
                content = payload["choices"][0]["message"]["content"]
            except (KeyError, IndexError) as e:
                raise GrokParseError(
                    f"xAI response shape unexpected: {payload}"
                ) from e
            try:
                return json.loads(content)
            except json.JSONDecodeError as e:
                raise GrokParseError(
                    f"xAI returned non-JSON content: {content[:300]}"
                ) from e

        # Loop exhausted (only reachable when the final attempt was 429 / transport)
        raise GrokAPIError(f"xAI request exhausted retries: {last_err}")
    finally:
        if owns_client and client is not None:
            client.close()


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------


def enrich_handle(
    handle: str,
    *,
    client: httpx.Client | None = None,
    timeout: float = 90.0,
    context: str | None = None,
) -> EnrichedHandle:
    """Live xAI lookup of a single X handle for the wizard preflight.

    Returns an :class:`EnrichedHandle`. Caller is responsible for wrapping it
    in a :class:`PreflightResponse` if the comparable-projects step also runs.

    Args:
        context: optional operator-supplied priming text (e.g. "TIG is a
            DeSci-adjacent algorithmic-bounty community ..."). Injected into
            the prompt so Grok can disambiguate when the public bio is thin.
    """
    h = _normalize(handle)
    api_key = _resolve_api_key()
    raw = _post_chat(
        prompt=_build_enrich_prompt(h, context=context),
        client=client,
        api_key=api_key,
        timeout=timeout,
    )
    raw["handle"] = h  # always trust our normalized form, not Grok's echo
    try:
        return EnrichedHandle.model_validate(raw)
    except ValidationError as e:
        raise GrokParseError(f"enrich_handle schema validation: {e}") from e


def suggest_comparable_projects(
    handle: str,
    themes: list[str],
    *,
    client: httpx.Client | None = None,
    timeout: float = 90.0,
    context: str | None = None,
    exclude_handles: list[str] | None = None,
    allow_non_crypto_research: bool = False,
    inclusion_hint: str | None = None,
    extra_exclusions: list[str] | None = None,
) -> list[ComparableProject]:
    """Live xAI suggestion of similar-audience projects on X.

    Args:
        context: optional operator-supplied priming text injected into the prompt.
        exclude_handles: handles Grok should not suggest (e.g. other Sable
            clients to avoid pool conflicts).
        allow_non_crypto_research: relax the "non-crypto consumer brands"
            exclusion for research labs / AI-ML / academic accounts. Useful
            for DeSci/AI-adjacent clients like TIG.
        inclusion_hint: operator-supplied positive bias for matches (e.g.
            "prefer accounts that have referenced AlphaEvolve-style algorithmic
            wins"). Renders as an INCLUSION HINTS block in the prompt.
        extra_exclusions: additional category-level exclusion rules (e.g.
            "Closed-source corporate AI accounts without an open-research angle").
            Each string becomes its own bullet under EXCLUSIONS.
    """
    h = _normalize(handle)
    api_key = _resolve_api_key()
    raw = _post_chat(
        prompt=_build_comparable_prompt(
            h, themes,
            context=context,
            exclude_handles=exclude_handles,
            allow_non_crypto_research=allow_non_crypto_research,
            inclusion_hint=inclusion_hint,
            extra_exclusions=extra_exclusions,
        ),
        client=client,
        api_key=api_key,
        timeout=timeout,
    )
    items = raw.get("comparable_projects") or []
    if not isinstance(items, list):
        raise GrokParseError(f"comparable_projects not a list: {type(items)}")
    out: list[ComparableProject] = []
    for item in items:
        try:
            cp = ComparableProject.model_validate(item)
        except ValidationError as e:
            raise GrokParseError(
                f"suggest_comparable schema validation: {e} (item: {item})"
            ) from e
        # Strip self-references defensively (the prompt forbids them but Grok
        # has been seen to ignore that instruction).
        if _normalize(cp.handle) == h:
            continue
        out.append(cp)
    return out


def build_preflight_response(
    handle: str,
    *,
    client: httpx.Client | None = None,
    enrich_timeout: float = 90.0,
    comparable_timeout: float = 90.0,
    context: str | None = None,
    exclude_handles: list[str] | None = None,
    allow_non_crypto_research: bool = False,
    inclusion_hint: str | None = None,
    extra_exclusions: list[str] | None = None,
) -> PreflightResponse:
    """Convenience wrapper used by the FastAPI sidecar's /preflight endpoint.

    Calls both ``enrich_handle`` and ``suggest_comparable_projects`` (using
    the freshly-derived themes so the operator gets a coherent first pass)
    and returns a single :class:`PreflightResponse` with one shared
    :class:`SignalMetadata` block.

    See ``enrich_handle`` and ``suggest_comparable_projects`` for the
    optional context / exclude_handles / allow_non_crypto_research /
    inclusion_hint / extra_exclusions semantics.
    """
    enriched = enrich_handle(
        handle, client=client, timeout=enrich_timeout, context=context,
    )
    comparables = suggest_comparable_projects(
        handle,
        enriched.recent_themes,
        client=client,
        timeout=comparable_timeout,
        context=context,
        exclude_handles=exclude_handles,
        allow_non_crypto_research=allow_non_crypto_research,
        inclusion_hint=inclusion_hint,
        extra_exclusions=extra_exclusions,
    )
    return PreflightResponse(
        **enriched.model_dump(),
        comparable_projects=comparables,
        signal_metadata=SignalMetadata(
            source="grok_xai_live",
            model=GROK_MODEL,
            fetched_at_utc=_now_iso(),
            signal_type="interpretive",
            caveat=(
                "AI-suggested via xAI Grok live X search; operator should "
                "confirm against on-platform context before submitting."
            ),
        ),
    )


def build_suggest_comparable_response(
    handle: str,
    themes: list[str],
    *,
    client: httpx.Client | None = None,
    timeout: float = 90.0,
) -> SuggestComparableResponse:
    """Standalone wrapper for the /suggest-comparable endpoint."""
    h = _normalize(handle)
    comparables = suggest_comparable_projects(
        h, themes, client=client, timeout=timeout
    )
    return SuggestComparableResponse(
        source_handle=h,
        comparable_projects=comparables,
        signal_metadata=SignalMetadata(
            source="grok_xai_live",
            model=GROK_MODEL,
            fetched_at_utc=_now_iso(),
            signal_type="interpretive",
            caveat=(
                "AI-suggested via xAI Grok live X search; operator should "
                "confirm against on-platform context before submitting."
            ),
        ),
    )


# Re-export for tests and callers that want to construct AxisPair directly
__all__ = [
    "AxisPair",
    "ComparableProject",
    "EnrichedHandle",
    "FIXED_AXIS_LIBRARY",
    "GROK_MODEL",
    "GrokAPIError",
    "GrokAuthError",
    "GrokParseError",
    "PreflightResponse",
    "SignalMetadata",
    "SuggestComparableResponse",
    "build_preflight_response",
    "build_suggest_comparable_response",
    "enrich_handle",
    "suggest_comparable_projects",
]
