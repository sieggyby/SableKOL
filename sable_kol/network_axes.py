"""Semantic-axis scoring for the network graph.

Per /grill-me Q5 (resolved 2026-05-07): each candidate gets a score in
[0, 1] for each configured axis. Score = matched-token-count / saturation,
plus optional per-archetype boost.

Generic field names — `axis_scores.x`, `axis_scores.y` — so the same
rendering code works for every client. SolStitch's x = "fashion-relevance",
y = "crypto-native-ness"; TIG's might be different. Labels live in the
client config, not in this module.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from sable_kol.client_config import AxisConfig, NetworkAxes


# Word-prefix regex used for tokenization. 3+ alphanumeric chars to avoid
# false matches on tiny tokens like "ai" colliding with "Ainsley".
_TOKEN_RE = re.compile(r"[a-z][a-z0-9_-]{2,}")


@dataclass(slots=True)
class CandidateLite:
    """Just the fields axis_score needs. Decouples from the full Candidate
    dataclass so tests don't need a full bank row."""
    bio: str | None
    display_name: str | None
    sector_tags: list[str]
    archetype_tags: list[str]


def _tokenize(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(text.lower())) if text else set()


def _theme_tokens(keywords: list[str]) -> set[str]:
    out: set[str] = set()
    for kw in keywords:
        for w in _TOKEN_RE.findall(kw.lower()):
            out.add(w)
    return out


def axis_score(candidate: CandidateLite, axis: AxisConfig) -> float:
    """Score one candidate against one axis.

    Combines:
      * bio + display_name + sector_tags + archetype_tags as searchable text
      * count of matched tokens vs axis.keywords (post-tokenization)
      * archetype_boosts (e.g., 'artist': +0.5 for the fashion axis)
      * saturated at 1.0

    Returns:
        Float in [0.0, 1.0].
    """
    if not axis.keywords:
        return 0.0
    blob = " ".join(filter(None, [
        candidate.bio or "",
        candidate.display_name or "",
        " ".join(candidate.sector_tags or []),
        " ".join(candidate.archetype_tags or []),
    ]))
    text_tokens = _tokenize(blob)
    theme_tokens = _theme_tokens(axis.keywords)

    matches = text_tokens & theme_tokens
    base = len(matches) / max(axis.saturation, 1)

    boost = 0.0
    for archetype in candidate.archetype_tags or []:
        b = axis.archetype_boosts.get(archetype)
        if b:
            boost += float(b)

    return min(1.0, base + boost)


def axis_scores(candidate: CandidateLite, axes: NetworkAxes) -> dict[str, float]:
    """Return ``{'x': float, 'y': float}`` for the two configured axes."""
    return {
        "x": axis_score(candidate, axes.x),
        "y": axis_score(candidate, axes.y),
    }
