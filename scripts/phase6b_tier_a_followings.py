"""Phase 6b — Tier-A override sweep.

Pulls followings of the 72 Tier-A candidates from
`~/Downloads/solstitch_outreach_plan_2026_05_06.json`, with these rules:

  * cap friends_count <= 10000 to skip news-aggregator / auto-follow noise
    (e.g. @zachboychuk has 652K friends — clearly not curated taste)
  * MANUAL_PINS override the cap (operator-validated must-pulls)
  * skip handles already covered by a completed kol_extract_runs row

After running, regenerate the kingmakers/clusters/outreach plan via
build_outreach_plan.py to incorporate the wider co-follow signal.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from sable_kol import socialdata_bulk as bulk
from sable_kol.db import open_db


PLAN_JSON = Path.home() / "Downloads" / "solstitch_outreach_plan_2026_05_06.json"
TIER_A_FRIENDS_CAP = 10000

# Mirror the operator-pinned set from build_outreach_plan.py.
MANUAL_PINS = {
    "zigor", "toomuchlag", "nanixbt", "auri_0x", "loomdart",
}


def main() -> None:
    with open(PLAN_JSON) as fh:
        plan = json.load(fh)
    tier_a = plan["targets"]["A"]
    print(f"Tier A: {len(tier_a)} candidates")

    with open_db() as conn:
        # Load known friends_counts in one shot.
        handles = [t["handle"] for t in tier_a]
        rows = conn.execute(
            "SELECT handle_normalized, twitter_id, following_count, followers_snapshot "
            "FROM kol_candidates "
            f"WHERE is_unresolved=0 AND handle_normalized IN ({','.join('?' * len(handles))})",
            handles,
        ).fetchall()
        bank = {r["handle_normalized"]: dict(r) for r in rows}

        # Already-pulled (from variant-2 sweep).
        already = {
            r["target_handle_normalized"]
            for r in conn.execute(
                "SELECT target_handle_normalized FROM kol_extract_runs "
                "WHERE extract_type='following' AND cursor_completed=1"
            ).fetchall()
        }

        # Build the actual cohort.
        cohort: list[dict] = []
        skipped_already: list[str] = []
        skipped_too_many: list[tuple[str, int]] = []
        for t in tier_a:
            h = t["handle"]
            if h in already:
                skipped_already.append(h)
                continue
            row = bank.get(h, {})
            fc = row.get("following_count")
            is_pin = h in MANUAL_PINS
            if fc is not None and fc > TIER_A_FRIENDS_CAP and not is_pin:
                skipped_too_many.append((h, fc))
                continue
            cohort.append({
                "handle": h,
                "twitter_id": row.get("twitter_id"),
                "following_count": fc,
                "is_pin": is_pin,
                "reach": t.get("reach_total"),
            })

        print(f"  skipped (already pulled): {len(skipped_already)}")
        print(f"  skipped (>10K friends, not pinned): {len(skipped_too_many)}")
        for h, fc in skipped_too_many[:8]:
            print(f"    @{h}: {fc} friends")
        print(f"  cohort to pull: {len(cohort)}")

        # Cost projection.
        unknown_count = sum(1 for c in cohort if c["following_count"] is None)
        known_sum = sum(c["following_count"] for c in cohort if c["following_count"] is not None)
        # Assume ~1500 avg for unknown (will discover via resolve).
        est_pages = (known_sum + unknown_count * 1500) / 49
        est_cost = est_pages * 0.002 + unknown_count * 0.002  # +profile-resolve calls
        print(f"  est total pages: {est_pages:.0f}")
        print(f"  est cost: ${est_cost:.2f}")
        print()
        print("--- pulling followings ---")

        completed = 0
        partial = 0
        skipped_runtime = 0
        total_edges = 0
        total_cost = 0.0
        t_start = time.monotonic()

        for i, c in enumerate(cohort, 1):
            uid = c["twitter_id"]
            if not uid:
                # Resolve from handle. One paid call (logged).
                try:
                    uid = bulk.resolve_user_id(conn, c["handle"])
                except Exception as exc:
                    print(f"[{i}/{len(cohort)}] @{c['handle']} — resolve error: {exc}")
                    skipped_runtime += 1
                    continue
                if not uid:
                    print(f"[{i}/{len(cohort)}] @{c['handle']} — could not resolve user_id")
                    skipped_runtime += 1
                    continue

            # Pin override: pass effectively-unlimited max_following so the
            # generator doesn't pre-skip.
            max_following = 1_000_000 if c["is_pin"] else TIER_A_FRIENDS_CAP

            run = bulk.create_run(
                conn,
                target_handle=c["handle"],
                target_user_id=uid,
                extract_type="following",
                expected_count=c["following_count"],
            )

            n = 0
            edge_batch: list[dict] = []
            try:
                for profile in bulk.pull_following(
                    conn,
                    run=run,
                    max_following=max_following,
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
                print(f"  [{i}/{len(cohort)}] @{c['handle']} FAILED: {type(exc).__name__}: {exc}")
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
            eta = (len(cohort) - i) / rate if rate > 0 else 0
            pin_marker = " [PIN]" if c["is_pin"] else ""
            print(f"[{i}/{len(cohort)}] @{c['handle']}{pin_marker} → {n} edges, "
                  f"{(final.pages_fetched if final else 0)} pages, "
                  f"${(final.cost_usd_logged if final else 0):.4f}; "
                  f"running total ${total_cost:.2f}, ETA {eta/60:.1f}min")

        print()
        print(f"--- summary ---")
        print(f"completed: {completed}")
        print(f"partial: {partial}")
        print(f"skipped at runtime: {skipped_runtime}")
        print(f"total edges inserted: {total_edges}")
        print(f"total cost logged: ${total_cost:.2f}")


if __name__ == "__main__":
    main()
