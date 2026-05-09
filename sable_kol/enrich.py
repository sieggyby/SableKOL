"""ETL Stage 5 — paid enrichment + KOL strength scoring.

Two modes:

* **`--score-only`** (FREE): recompute ``kol_strength_score`` from existing
  fields only. Useful after multi-list scrapes — the ``list:`` discovery_sources
  count grows, so the score should be refreshed without paying for SocialData
  again.

* **default** (PAID): for each unenriched live candidate, call SocialData
  ``GET /twitter/user/{handle}`` once. Writes back: ``twitter_id``, an
  upgraded ``bio_snapshot``, ``followers_snapshot``, ``verified``,
  ``account_created_at``. Then recomputes ``kol_strength_score``. Cost ≈
  ``$0.0002 × candidates``. TTL is governed by ``last_enriched_at`` (7 days,
  matching ``project_profiles_external``); ``--refresh`` ignores TTL.

The enrichment fetcher is injectable for tests (same pattern as
``profile.build_external_profile``). In production it imports Slopper's
``socialdata_get`` from the ``[paid-enrich]`` extra.
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from sable_kol import cost as cost_mod
from sable_kol.db import (
    Candidate,
    list_candidates,
    open_db,
)


logger = logging.getLogger(__name__)

# 7 days, mirrors profile.PAID_PROFILE_TTL_SECONDS.
ENRICH_TTL_SECONDS = 7 * 24 * 60 * 60


SocialDataFetcher = Callable[[str], dict]
"""Callable that returns a profile dict for a handle. Default uses Slopper."""


# ---------------------------------------------------------------------------
# Score formula
# ---------------------------------------------------------------------------

def compute_kol_strength(c: Candidate) -> float:
    """Project-independent KOL strength in [0, 1].

    Formula (weights sum to 1.0):
      0.50 · followers_score      log10(max_fc) mapped from [3, 6.5] → [0, 1]
                                  where max_fc = max(twitter, IG, TikTok, threads,
                                                     YouTube subs, etc) — fashion
                                                     KOLs often have IG >> Twitter
      0.40 · list_vote_score      sum of curator weights from list:* sources,
                                  capped at 5.0 weighted votes
      0.10 · verified_bonus       1 if verified else 0

    Per-curator weights live in ``~/.sable/kol_list_curators.yaml``. Editorial
    directories like coinlaunch_space can score 2.0 per appearance; unknown
    curators default to 1.0. See ``sable_kol/curators.py``.

    Cross-platform reach is now incorporated: when ``platform_presence_json``
    contains larger audiences on IG/TikTok/Threads/YouTube, the maximum is used
    in followers_score. This corrects the systematic under-ranking of fashion
    and lifestyle KOLs whose audience is IG-primary.

    When followers_snapshot is null (pre-enrichment), followers_score is 0
    and the score still works off list_vote_score + verified.
    """
    from sable_kol.curators import weight_for_list_source

    # Aggregate followers across X + cross-platform presence.
    fc_x = c.followers_snapshot or 0
    fc_max = fc_x
    pp = getattr(c, "platform_presence", None) or {}
    for plat_name, plat_data in pp.items():
        if not isinstance(plat_data, dict):
            continue
        # Platforms use "followers" (IG/TT/Threads/Lens/Farcaster) or "subscribers" (YouTube/Substack).
        f = plat_data.get("followers") or plat_data.get("subscribers") or 0
        if isinstance(f, (int, float)) and f > fc_max:
            fc_max = int(f)

    if fc_max <= 0:
        followers_score = 0.0
    else:
        log_fc = math.log10(max(1, fc_max))
        # 1K (log=3) → 0; 3.16M (log=6.5) → 1.0; clamp to [0,1].
        followers_score = max(0.0, min(1.0, (log_fc - 3.0) / 3.5))

    list_weight = sum(
        weight_for_list_source(s)
        for s in c.discovery_sources
        if s.startswith("list:")
    )
    list_vote_score = min(1.0, list_weight / 5.0)

    verified_bonus = 1.0 if (c.is_unresolved == 0 and bool(c.verified)) else 0.0

    return round(
        0.50 * followers_score
        + 0.40 * list_vote_score
        + 0.10 * verified_bonus,
        4,
    )


# ---------------------------------------------------------------------------
# Score-only pass (no paid calls)
# ---------------------------------------------------------------------------

@dataclass
class ScoreSummary:
    rescored: int = 0


def run_score_only() -> ScoreSummary:
    """Recompute kol_strength_score for every live candidate. No paid calls."""
    summary = ScoreSummary()
    with open_db() as conn:
        candidates = _list_candidates_with_extras(conn)
        for c in candidates:
            score = compute_kol_strength(c)
            conn.execute(
                "UPDATE kol_candidates SET kol_strength_score = :s "
                "WHERE candidate_id = :cid",
                {"s": score, "cid": c.candidate_id},
            )
            summary.rescored += 1
        conn.commit()
    return summary


# ---------------------------------------------------------------------------
# Paid pass — SocialData /twitter/user/{handle} per candidate
# ---------------------------------------------------------------------------

@dataclass
class EnrichSummary:
    enriched: int = 0
    skipped_fresh: int = 0
    errors: int = 0
    cost_usd: float = 0.0
    rescored: int = 0


def _is_stale(last_enriched_at: str | None, ttl_seconds: int) -> bool:
    if not last_enriched_at:
        return True
    try:
        dt = datetime.fromisoformat(last_enriched_at.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    age = (datetime.now(timezone.utc) - dt).total_seconds()
    return age > ttl_seconds


def run_enrich(
    *,
    limit: int | None = None,
    refresh: bool = False,
    socialdata_fetcher: SocialDataFetcher | None = None,
) -> EnrichSummary:
    """Paid enrichment pass + score recompute. Idempotent within TTL."""
    summary = EnrichSummary()
    with open_db() as conn:
        candidates = _list_candidates_with_extras(conn, limit=limit)
        if socialdata_fetcher is None and any(
            refresh or _is_stale(c.last_enriched_at, ENRICH_TTL_SECONDS)
            for c in candidates
        ):
            socialdata_fetcher = _default_socialdata_fetcher

        for c in candidates:
            stale = refresh or _is_stale(c.last_enriched_at, ENRICH_TTL_SECONDS)
            if not stale:
                summary.skipped_fresh += 1
                # Still recompute score in case multi-list signals changed.
                score = compute_kol_strength(c)
                conn.execute(
                    "UPDATE kol_candidates SET kol_strength_score = :s "
                    "WHERE candidate_id = :cid",
                    {"s": score, "cid": c.candidate_id},
                )
                summary.rescored += 1
                continue

            try:
                data = socialdata_fetcher(c.handle_normalized)  # type: ignore[misc]
            except Exception as exc:
                logger.warning("enrich failed for %s: %s", c.handle_normalized, exc)
                summary.errors += 1
                cost_mod.record(
                    conn,
                    org_id=None,
                    call_type="socialdata_user_profile",
                    cost_usd=0.0,
                    call_status="error",
                )
                continue

            twitter_id = (
                data.get("id_str")
                or (str(data["id"]) if data.get("id") is not None else None)
            )
            bio = data.get("description") or data.get("bio") or c.bio_snapshot
            followers = (
                data.get("followers_count")
                or data.get("followers")
                or c.followers_snapshot
            )
            verified_flag = 1 if (
                bool(data.get("verified"))
                or bool(data.get("is_blue_verified"))
                or bool(data.get("is_verified"))
            ) else 0
            created_at = data.get("created_at")

            # Update the row.
            conn.execute(
                "UPDATE kol_candidates SET "
                "  twitter_id = COALESCE(:tid, twitter_id), "
                "  bio_snapshot = COALESCE(:bio, bio_snapshot), "
                "  followers_snapshot = COALESCE(:fc, followers_snapshot), "
                "  verified = :vf, "
                "  account_created_at = COALESCE(:ca, account_created_at), "
                "  last_enriched_at = CURRENT_TIMESTAMP, "
                "  enrichment_tier = 'basic' "
                "WHERE candidate_id = :cid",
                {
                    "tid": twitter_id,
                    "bio": bio,
                    "fc": followers,
                    "vf": verified_flag,
                    "ca": created_at,
                    "cid": c.candidate_id,
                },
            )
            conn.commit()

            # Re-fetch the row, then score. We do a tiny re-read here to keep
            # compute_kol_strength's input shape consistent.
            updated = _row_for_id(conn, c.candidate_id)
            score = compute_kol_strength(updated)
            conn.execute(
                "UPDATE kol_candidates SET kol_strength_score = :s "
                "WHERE candidate_id = :cid",
                {"s": score, "cid": c.candidate_id},
            )
            conn.commit()

            cost_mod.record(
                conn,
                org_id=None,
                call_type="socialdata_user_profile",
                cost_usd=0.0002,
            )
            summary.enriched += 1
            summary.cost_usd += 0.0002
            summary.rescored += 1

    return summary


# ---------------------------------------------------------------------------
# Helpers — Candidate dataclass doesn't yet carry verified/account_created_at,
# so for enrich we read the full row directly.
# ---------------------------------------------------------------------------

def _list_candidates_with_extras(conn: Any, limit: int | None = None) -> list[Candidate]:
    """Live + active rows ordered by candidate_id."""
    from sable_kol.db import _row_to_candidate
    sql = (
        "SELECT * FROM kol_candidates "
        "WHERE is_unresolved = 0 AND status = 'active' "
        "ORDER BY candidate_id"
    )
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql, {}).fetchall()
    return [_row_to_candidate(r) for r in rows]


def _row_for_id(conn: Any, candidate_id: int) -> Candidate:
    from sable_kol.db import _row_to_candidate
    row = conn.execute(
        "SELECT * FROM kol_candidates WHERE candidate_id = :cid",
        {"cid": candidate_id},
    ).fetchone()
    return _row_to_candidate(row)


# ---------------------------------------------------------------------------
# Production fetcher — Slopper-backed
# ---------------------------------------------------------------------------

def _default_socialdata_fetcher(handle: str) -> dict:
    try:
        from sable.shared.socialdata import socialdata_get  # type: ignore[import-not-found]
    except ImportError as e:
        raise RuntimeError(
            "enrich requires Slopper. Install with: pip install -e '.[paid-enrich]'"
        ) from e
    return socialdata_get(f"/twitter/user/{handle}")
