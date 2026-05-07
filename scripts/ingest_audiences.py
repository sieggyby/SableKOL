"""Phase 3 — ingest the Doji / 9dcc / Fabricant audience JSONL into the bank.

For each JSONL file:
  * upsert_candidate per profile with the configured discovery_source
  * backfill twitter_id, followers_snapshot, friends_count → following_count,
    location, listed_count, tweets_count, account_created_at, verified —
    these come from SocialData responses already on disk and are free.
  * dedupe across the three audiences via the partial unique index on
    handle_normalized; multi-source rows accumulate discovery_sources_json
    automatically through the upsert path.

After ingest, prints a summary:
  * counts per source (parsed / inserted / updated / skipped / conflicts)
  * cross-source overlap (handles that appear in 2+ audiences)
  * count with friends_count<1000 (the Phase 6 candidate cohort)

Usage:
    .venv/bin/python scripts/ingest_audiences.py
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from sable_kol.db import normalize_handle, open_db, upsert_candidate


REPO = Path(__file__).resolve().parent.parent
GROK = REPO / "grok_responses"

DATE_SLUG = "2026_05_06"

AUDIENCES = [
    {
        "label": "doji_audience",
        "jsonl": GROK / f"audience_doji_com_{DATE_SLUG}.jsonl",
        "source_id": f"list:doji_audience:{DATE_SLUG}",
    },
    {
        "label": "9dcc_audience",
        "jsonl": GROK / f"audience_9dccxyz_{DATE_SLUG}.jsonl",
        "source_id": f"list:9dcc_audience:{DATE_SLUG}",
    },
    {
        "label": "fabricant_audience",
        "jsonl": GROK / f"audience_thefabricant_{DATE_SLUG}.jsonl",
        "source_id": f"list:fabricant_audience:{DATE_SLUG}",
    },
]


def _profile_key(p: dict) -> str | None:
    sn = p.get("screen_name")
    if not sn:
        return None
    return normalize_handle(sn)


def _parse_x_created_at(s: str | None) -> str | None:
    """Convert SocialData created_at (e.g. 'Mon May 25 17:43:17 +0000 2009') to ISO."""
    if not s:
        return None
    for fmt in ("%a %b %d %H:%M:%S %z %Y", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(s, fmt).isoformat()
        except ValueError:
            continue
    return None  # leave NULL on unknown format


def main() -> None:
    seen_per_source: dict[str, set[str]] = defaultdict(set)
    summary = {a["label"]: {"parsed": 0, "inserted": 0, "updated": 0, "conflicts": 0, "skipped": 0}
               for a in AUDIENCES}

    with open_db() as conn:
        for aud in AUDIENCES:
            label = aud["label"]
            print(f"\n=== {label} ({aud['jsonl'].name}) ===")
            if not aud["jsonl"].exists():
                print(f"  MISSING: {aud['jsonl']}")
                continue

            with open(aud["jsonl"], "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        p = json.loads(line)
                    except Exception:
                        summary[label]["skipped"] += 1
                        continue
                    summary[label]["parsed"] += 1
                    h = _profile_key(p)
                    if not h:
                        summary[label]["skipped"] += 1
                        continue
                    seen_per_source[label].add(h)

                    twitter_id = p.get("id_str") or (str(p["id"]) if p.get("id") is not None else None)
                    bio = p.get("description") or None
                    followers = p.get("followers_count")
                    name = p.get("name") or None

                    try:
                        res = upsert_candidate(
                            conn,
                            handle=h,
                            display_name=name,
                            bio_snapshot=bio,
                            followers_snapshot=followers,
                            discovery_source=aud["source_id"],
                            twitter_id=twitter_id,
                        )
                    except Exception:
                        summary[label]["skipped"] += 1
                        continue

                    # Backfill enrichment columns directly. We have these for
                    # free from the SocialData payload; no separate paid call
                    # needed. Skip if already populated (manual review may
                    # have set them).
                    conn.execute(
                        "UPDATE kol_candidates SET "
                        "  following_count   = COALESCE(following_count, :fr), "
                        "  listed_count      = COALESCE(listed_count, :lc), "
                        "  tweets_count      = COALESCE(tweets_count, :tc), "
                        "  location          = COALESCE(location, :loc), "
                        "  account_created_at = COALESCE(account_created_at, :ac), "
                        "  verified          = CASE WHEN verified=0 AND :v=1 THEN 1 ELSE verified END "
                        "WHERE candidate_id = :cid",
                        {
                            "fr": p.get("friends_count"),
                            "lc": p.get("listed_count"),
                            "tc": p.get("statuses_count"),
                            "loc": p.get("location") or None,
                            "ac": _parse_x_created_at(p.get("created_at")),
                            "v": 1 if p.get("verified") else 0,
                            "cid": res.candidate_id,
                        },
                    )

                    if res.conflicted:
                        summary[label]["conflicts"] += 1
                    elif res.inserted:
                        summary[label]["inserted"] += 1
                    elif res.updated:
                        summary[label]["updated"] += 1
                conn.commit()

            s = summary[label]
            print(f"  parsed={s['parsed']} inserted={s['inserted']} updated={s['updated']} "
                  f"conflicts={s['conflicts']} skipped={s['skipped']}")

        # ----- Overlap & cohort summary -----
        print("\n=== overlap ===")
        all_sources = list(seen_per_source.keys())
        all_handles = set().union(*seen_per_source.values())
        print(f"unique handles across all 3 audiences: {len(all_handles)}")
        for src in all_sources:
            print(f"  {src}: {len(seen_per_source[src])} unique")
        overlap_2plus = sum(
            1 for h in all_handles
            if sum(1 for s in all_sources if h in seen_per_source[s]) >= 2
        )
        overlap_3 = sum(
            1 for h in all_handles
            if all(h in seen_per_source[s] for s in all_sources)
        )
        print(f"in ≥2 audiences (multi-source vote): {overlap_2plus}")
        print(f"in all 3 audiences: {overlap_3}")

        print("\n=== Phase 6 cohort gates ===")
        # Variant 2 from the user's question: friends_count < 1000 AND
        # followers_count >= 10K AND in our audience set. Sector filter
        # waits for Phase 3 classify.
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM kol_candidates "
            "WHERE is_unresolved=0 AND following_count IS NOT NULL "
            "AND following_count < 1000"
        ).fetchone()
        print(f"following_count<1000 (loose Phase 6 cohort): {row['n']}")
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM kol_candidates "
            "WHERE is_unresolved=0 AND following_count IS NOT NULL "
            "AND following_count < 1000 AND followers_snapshot >= 10000"
        ).fetchone()
        print(f"following_count<1000 AND followers>=10K (variant 2 gate): {row['n']}")
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM kol_candidates "
            "WHERE is_unresolved=0 AND following_count IS NOT NULL "
            "AND following_count < 500 AND followers_snapshot >= 5000"
        ).fetchone()
        print(f"following_count<500 AND followers>=5K (tight gate): {row['n']}")


if __name__ == "__main__":
    main()
