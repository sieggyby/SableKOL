"""Tiered outreach plan production for the SolStitch (and future) campaign.

Implements Phase 8 of the SolStitch outreach plan (v3): given a project's
ranked KOL pool and follow-graph clustering, produce a 200-row plan split
across Tier-A high-touch / Tier-B warm-DM / Tier-C templated cohorts.

Key design points (per the plan's audit pass):

* **Best-of-platform reach**: each candidate's tier is decided by
  ``max(X, IG, TikTok, YouTube)``, NOT just X-followers. ``primary_platform``
  records which won so the operator knows where to DM.
* **Social-proximity vs operator-confirmed intros are separate fields.**
  ``social_proximity_brokers`` is the X co-follow signal (priors only —
  co-follow ≠ willingness to introduce). ``operator_confirmed_intros`` is
  manual operator annotation, populated post-build by the operator with
  actual relationships they have personally vetted.
* **Operator-pin override** lets a below-threshold candidate be promoted to
  Tier-A when the operator marks them must-have for the campaign.
* The follow-graph artifacts are *optional*: if no co-follow matrix is
  provided, cluster_id / cluster_label / social_proximity_brokers are
  empty and the plan still produces tier assignments.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

from sable_kol.db import Candidate, list_candidates, open_db
from sable_kol.follow_graph import (
    Cluster,
    CoFollowMatrix,
    cluster_label_via_tfidf,
    map_social_proximity,
)


# ---------------------------------------------------------------------------
# Tier defaults (configurable per call to build_plan)
# ---------------------------------------------------------------------------

DEFAULT_TIER_A_THRESHOLD = 100_000
DEFAULT_TIER_B_THRESHOLD = 10_000
DEFAULT_TIER_C_THRESHOLD = 1_000

# Platforms we consider for "best-of" reach computation. Order matches the
# common-priority ordering in the SolStitch thesis (X-first for crypto, then
# IG/TikTok/YT for fashion-cultural reach).
REACH_PLATFORMS = ("x", "instagram", "tiktok", "youtube", "threads")


# ---------------------------------------------------------------------------
# Data shape
# ---------------------------------------------------------------------------

@dataclass
class OutreachTarget:
    handle: str
    tier: str                                       # 'A' | 'B' | 'C' | 'unranked'
    cluster_id: int | None = None
    cluster_label: str | None = None
    reach_total: int = 0                            # max across REACH_PLATFORMS
    primary_platform: str = "x"                     # which platform won the max
    score: float = 0.0                              # candidate.kol_strength_score (or 0)
    social_proximity_brokers: list[str] = field(default_factory=list)
    operator_confirmed_intros: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)  # discovery_sources_json
    archetype: str = ""                             # primary archetype tag
    suggested_theme: str = ""                       # SolStitch / project thematic angle
    notes: str = ""
    manual_pin: bool = False
    candidate_id: int | None = None


# ---------------------------------------------------------------------------
# Reach + platform helpers
# ---------------------------------------------------------------------------

def best_of_platform_reach(candidate: Candidate) -> tuple[int, str]:
    """Return ``(max_reach, winning_platform)`` for a candidate.

    The X-side reach comes from ``followers_snapshot`` (since X-data lives on
    that column historically). Other platforms come from
    ``platform_presence_json`` keyed by platform name. Missing platforms are
    treated as 0.
    """
    presence = candidate.platform_presence or {}
    candidates: list[tuple[int, str]] = []
    x_followers = candidate.followers_snapshot or 0
    candidates.append((int(x_followers), "x"))
    for plat in REACH_PLATFORMS:
        if plat == "x":
            continue
        info = presence.get(plat) or {}
        f = int(info.get("followers") or 0)
        if f:
            candidates.append((f, plat))
    if not candidates:
        return (0, "x")
    candidates.sort(key=lambda t: -t[0])
    return candidates[0]


def assign_tier(
    reach_total: int,
    *,
    tier_a_threshold: int = DEFAULT_TIER_A_THRESHOLD,
    tier_b_threshold: int = DEFAULT_TIER_B_THRESHOLD,
    tier_c_threshold: int = DEFAULT_TIER_C_THRESHOLD,
    manual_pin: bool = False,
) -> str:
    """Tier from cross-platform reach. ``manual_pin`` forces Tier-A."""
    if manual_pin:
        return "A"
    if reach_total >= tier_a_threshold:
        return "A"
    if reach_total >= tier_b_threshold:
        return "B"
    if reach_total >= tier_c_threshold:
        return "C"
    return "unranked"


# ---------------------------------------------------------------------------
# Cluster annotation
# ---------------------------------------------------------------------------

def _build_cluster_index(
    clusters: list[Cluster] | None,
    matrix: CoFollowMatrix | None,
) -> dict[str, tuple[int, str]]:
    """Map handle → (cluster_id, cluster_label) using TF-IDF labeling."""
    if not clusters or matrix is None:
        return {}
    out: dict[str, tuple[int, str]] = {}
    for c in clusters:
        label = c.label or cluster_label_via_tfidf(c.members, matrix)
        for h in c.members:
            out[h.lower().strip()] = (c.cluster_id, label)
    return out


# ---------------------------------------------------------------------------
# Plan builder
# ---------------------------------------------------------------------------

def build_plan(
    conn: Any,
    *,
    candidates: list[Candidate] | None = None,
    top_k: int = 200,
    tier_a_threshold: int = DEFAULT_TIER_A_THRESHOLD,
    tier_b_threshold: int = DEFAULT_TIER_B_THRESHOLD,
    tier_c_threshold: int = DEFAULT_TIER_C_THRESHOLD,
    co_follow_matrix: CoFollowMatrix | None = None,
    clusters: list[Cluster] | None = None,
    manual_pins: set[str] | None = None,
    suggested_theme_for_handle: dict[str, str] | None = None,
) -> list[OutreachTarget]:
    """Produce a tiered outreach plan from a candidate pool.

    If ``candidates`` is None, pulls all live + classified rows from the bank
    (via :func:`list_candidates`) and ranks them by ``kol_strength_score``
    descending, taking ``top_k``.

    ``co_follow_matrix`` + ``clusters`` enable cluster annotations and
    social-proximity broker mapping. Without them, those fields stay empty.

    ``manual_pins`` is a set of normalized handles the operator wants forced
    to Tier-A regardless of reach.

    ``suggested_theme_for_handle`` lets the caller inject per-handle theme
    angles (e.g. from a separate Haiku pass over candidate evidence).
    """
    pin = {h.lower().strip() for h in (manual_pins or set())}
    theme_map = suggested_theme_for_handle or {}

    if candidates is None:
        rows = list_candidates(conn, status="active", only_classified=True)
        rows.sort(
            key=lambda c: (c.kol_strength_score is None, -(c.kol_strength_score or 0.0)),
        )
        candidates = rows[:top_k]

    cluster_idx = _build_cluster_index(clusters, co_follow_matrix)

    # Build the pool of "broker candidates" — the row handles in the matrix.
    # Social-proximity brokers must be drawn from this pool, not from the
    # plan itself (the plan members may not have had their followings pulled).
    broker_pool: list[str] = list(co_follow_matrix.rows) if co_follow_matrix else []

    out: list[OutreachTarget] = []
    for c in candidates:
        h = c.handle_normalized
        manual_pin = h in pin
        reach, primary = best_of_platform_reach(c)
        tier = assign_tier(
            reach,
            tier_a_threshold=tier_a_threshold,
            tier_b_threshold=tier_b_threshold,
            tier_c_threshold=tier_c_threshold,
            manual_pin=manual_pin,
        )

        cluster_id: int | None = None
        cluster_label: str | None = None
        if h in cluster_idx:
            cluster_id, cluster_label = cluster_idx[h]

        brokers: list[str] = []
        if co_follow_matrix is not None and broker_pool:
            sp = map_social_proximity(h, broker_pool, co_follow_matrix)
            brokers = sp.brokers[:5]  # cap to avoid noisy lists in tier-C output

        archetype = c.archetype_tags[0] if c.archetype_tags else ""

        out.append(
            OutreachTarget(
                handle=h,
                tier=tier,
                cluster_id=cluster_id,
                cluster_label=cluster_label,
                reach_total=reach,
                primary_platform=primary,
                score=c.kol_strength_score or 0.0,
                social_proximity_brokers=brokers,
                operator_confirmed_intros=[],  # populated manually post-build
                sources=list(c.discovery_sources or []),
                archetype=archetype,
                suggested_theme=theme_map.get(h, ""),
                notes="",
                manual_pin=manual_pin,
                candidate_id=c.candidate_id,
            )
        )

    return out


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def to_json_payload(targets: list[OutreachTarget]) -> dict:
    """Serialize a plan to a deterministic JSON-friendly dict."""
    by_tier: dict[str, list[dict]] = {"A": [], "B": [], "C": [], "unranked": []}
    for t in targets:
        by_tier.setdefault(t.tier, []).append(asdict(t))
    return {
        "summary": {
            "total": len(targets),
            "tier_A": len(by_tier["A"]),
            "tier_B": len(by_tier["B"]),
            "tier_C": len(by_tier["C"]),
            "unranked": len(by_tier["unranked"]),
        },
        "targets": by_tier,
    }


def to_csv_rows(targets: list[OutreachTarget]) -> list[dict]:
    """Return CSV-ready row dicts (Tier-C templated workflow expects CSV).

    Columns: handle, tier, cluster, archetype, primary_platform, reach,
    theme_angle, suggested_template_id (cluster-id-based), score.
    """
    rows: list[dict] = []
    for t in targets:
        rows.append({
            "handle": t.handle,
            "tier": t.tier,
            "cluster_id": t.cluster_id if t.cluster_id is not None else "",
            "cluster_label": t.cluster_label or "",
            "archetype": t.archetype,
            "primary_platform": t.primary_platform,
            "reach": t.reach_total,
            "theme_angle": t.suggested_theme,
            "suggested_template_id": (
                f"cluster_{t.cluster_id}" if t.cluster_id is not None else "default"
            ),
            "score": t.score,
            "social_proximity_brokers": "|".join(t.social_proximity_brokers),
            "operator_confirmed_intros": "|".join(t.operator_confirmed_intros),
        })
    return rows
