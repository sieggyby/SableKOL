"""Phase 6 (variant 2) — pull followings for the curated cohort.

Cohort gate (post-classify):
  * is_unresolved=0
  * following_count IS NOT NULL AND following_count < <max-following>
  * followers_snapshot >= <min-followers>
  * sector_tags_json contains at least one of:
      'fashion', 'culture', 'art', 'design', 'nfts', 'creator', 'streetwear'
  * Skip any candidate already covered by a completed kol_extract_runs row
    (idempotent resume across multi-day runs).

Each pulled following list is persisted as kol_follow_edges rows with a
parent kol_extract_runs row. Cost is logged per page.

Usage:
    .venv/bin/python scripts/phase6_extract_followings.py [--max-following 1000]
                                                          [--min-followers 10000]
                                                          [--limit N]
                                                          [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from sable_kol import socialdata_bulk as bulk
from sable_kol.db import open_db


SECTOR_RELEVANT = (
    "fashion", "culture", "art", "design", "nfts", "creator",
    "streetwear", "music", "media",
)


def select_cohort(
    conn,
    *,
    max_following: int,
    min_followers: int,
    sector_filter: bool,
) -> list[dict]:
    """Return cohort rows (ordered by followers desc for diagnostic readability)."""
    rows = conn.execute(
        "SELECT candidate_id, handle_normalized, twitter_id, followers_snapshot, "
        "       following_count, sector_tags_json, archetype_tags_json "
        "FROM kol_candidates "
        "WHERE is_unresolved=0 "
        "  AND following_count IS NOT NULL "
        "  AND following_count < :mf "
        "  AND followers_snapshot >= :minf "
        "  AND status='active' "
        "ORDER BY followers_snapshot DESC NULLS LAST",
        {"mf": max_following, "minf": min_followers},
    ).fetchall()

    out: list[dict] = []
    for r in rows:
        sectors = set(json.loads(r["sector_tags_json"] or "[]"))
        if sector_filter:
            if not sectors.intersection(SECTOR_RELEVANT):
                continue
        out.append({
            "candidate_id": r["candidate_id"],
            "handle": r["handle_normalized"],
            "twitter_id": r["twitter_id"],
            "followers": r["followers_snapshot"],
            "following": r["following_count"],
            "sectors": sorted(sectors),
            "archetypes": json.loads(r["archetype_tags_json"] or "[]"),
        })
    return out


def already_pulled(conn, *, handle: str) -> bool:
    """Skip if there's already a completed 'following' run for this handle."""
    row = conn.execute(
        "SELECT 1 AS x FROM kol_extract_runs "
        "WHERE target_handle_normalized = :h AND extract_type='following' "
        "  AND cursor_completed = 1 "
        "LIMIT 1",
        {"h": handle},
    ).fetchone()
    return row is not None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-following", type=int, default=1000)
    ap.add_argument("--min-followers", type=int, default=10000)
    ap.add_argument("--limit", type=int, default=None,
                    help="Cap total cohort size (default: no cap).")
    ap.add_argument("--no-sector-filter", action="store_true",
                    help="Skip the sector_tags relevance filter (variant 3).")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print the cohort without making any paid calls.")
    args = ap.parse_args()

    with open_db() as conn:
        cohort = select_cohort(
            conn,
            max_following=args.max_following,
            min_followers=args.min_followers,
            sector_filter=not args.no_sector_filter,
        )
        print(f"cohort size pre-dedupe: {len(cohort)}")

        # Skip already-pulled handles.
        fresh: list[dict] = []
        skipped = 0
        for c in cohort:
            if already_pulled(conn, handle=c["handle"]):
                skipped += 1
                continue
            fresh.append(c)
        print(f"already-pulled (skipped): {skipped}")
        print(f"fresh cohort: {len(fresh)}")

        if args.limit:
            fresh = fresh[:args.limit]
            print(f"limit applied: {len(fresh)}")

        # Cost projection.
        avg_following = sum(c["following"] for c in fresh) / max(len(fresh), 1)
        est_pages = sum(c["following"] for c in fresh) / 49
        est_cost = est_pages * 0.002
        print(f"avg following per cohort member: {avg_following:.0f}")
        print(f"est total pages: {est_pages:.0f}")
        print(f"est cost: ${est_cost:.2f}")

        if args.dry_run:
            print("\n--- top 20 cohort members (dry-run) ---")
            for c in fresh[:20]:
                print(f"  @{c['handle']} followers={c['followers']} following={c['following']} "
                      f"sectors={c['sectors']} archetypes={c['archetypes']}")
            return

        # Execute.
        print("\n--- pulling followings ---")
        completed = 0
        partial = 0
        total_edges = 0
        total_cost = 0.0
        t_start = time.monotonic()

        for i, c in enumerate(fresh, 1):
            uid = c["twitter_id"]
            if not uid:
                # Resolve from handle. One paid call.
                uid = bulk.resolve_user_id(conn, c["handle"])
                if not uid:
                    print(f"[{i}/{len(fresh)}] @{c['handle']} — could not resolve user_id, skipping")
                    continue

            run = bulk.create_run(
                conn,
                target_handle=c["handle"],
                target_user_id=uid,
                extract_type="following",
                expected_count=c["following"],
            )

            n = 0
            edge_batch: list[dict] = []
            try:
                for profile in bulk.pull_following(
                    conn,
                    run=run,
                    max_following=args.max_following,
                ):
                    n += 1
                    edge_batch.append({
                        "follower_id": uid,
                        "follower_handle": c["handle"],
                        "followed_id": profile.get("id_str") or str(profile["id"]),
                        "followed_handle": profile.get("screen_name"),
                    })
                    if len(edge_batch) >= 100:
                        bulk.insert_edges(conn, run_id=run.run_id, edges=edge_batch)
                        edge_batch.clear()
                if edge_batch:
                    bulk.insert_edges(conn, run_id=run.run_id, edges=edge_batch)
            except Exception as exc:
                # mark_run_failed already called inside _paginate; surface and continue
                # to next cohort member rather than abort the whole batch.
                print(f"  [{i}/{len(fresh)}] @{c['handle']} FAILED: {type(exc).__name__}: {exc}")
                partial += 1
                continue

            final = bulk.get_run(conn, run.run_id)
            if final and final.cursor_completed:
                completed += 1
            else:
                partial += 1
            total_edges += n
            total_cost += (final.cost_usd_logged if final else 0.0)
            elapsed = time.monotonic() - t_start
            rate = i / elapsed if elapsed > 0 else 0
            eta = (len(fresh) - i) / rate if rate > 0 else 0
            print(f"[{i}/{len(fresh)}] @{c['handle']} → {n} edges, "
                  f"{(final.pages_fetched if final else 0)} pages, "
                  f"${(final.cost_usd_logged if final else 0):.4f}; "
                  f"running total ${total_cost:.2f}, ETA {eta/60:.1f}min")

        print(f"\n--- summary ---")
        print(f"completed: {completed}")
        print(f"partial:   {partial}")
        print(f"total edges inserted: {total_edges}")
        print(f"total cost logged: ${total_cost:.2f}")


if __name__ == "__main__":
    main()
