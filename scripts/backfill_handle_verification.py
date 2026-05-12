"""KO-5: backfill SocialData handle verification on historical kol_candidates.

Pre-2026-05-10, ``suggest_comparable_projects()`` trusted Grok's
self-reported ``handle_verified=true``. Empirically that lies on
~20-50% of suggestions (e.g. ``bittensor_`` / ``eleutherai`` /
``gensynnetwork`` were hallucinated on the first two TIG runs).
``handle_verifier`` shipped in commit ``db11685`` gates new calls; this
script ground-truth's the historical rows.

Risk model. The realistic hallucination signal in the existing bank is
``bio_snapshot IS NULL`` — a real X account has a bio (even empty
bios get fetched). Handles ingested from X-list exports always have
bios; suspect rows are the ones that came in via paths that don't
hydrate bio. ``--filter risky`` (default) targets these. ``--filter
unverified`` targets every row where ``twitter_id IS NULL`` (broader,
~3.6k rows in prod as of 2026-05-12). ``--filter all`` walks every
active live candidate (~17k, ~$3.50 at SocialData).

Outcomes:

* **ALIVE**       SocialData returns 200 with profile data. No-op.
* **NOT_FOUND**   SocialData 404 / 410 OR returns 200 with
                   ``{"status": "error", "message": "User not found"}``.
                   Action: soft-archive (``status='archived'``) +
                   append ``kol_graph:archived_by_ko5:<date>`` to
                   ``discovery_sources_json`` so the change is
                   identifiable / reversible.
* **SUSPENDED**   Detected via SocialData's status-error message.
                   Same archive action.
* **ERROR**       Network / 5xx after retries / parse error. Skip
                   without archiving — fail-open to avoid losing real
                   handles to transient SocialData weather.

Defaults to dry-run. Pass ``--apply`` to write. Audit log entries are
written per archived row regardless.

Cost. ~$0.0002 per row. Default ``risky`` filter is bounded to ~few
dozen rows so the spend is negligible (~$0.01). ``--filter all``
costs ~$3.50 — within the original TODO estimate.

Usage:

    .venv/bin/python scripts/backfill_handle_verification.py [--apply]
            [--filter risky|unverified|all]
            [--limit N]
            [--batch-size 50]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from sable_kol.db import open_db
from sable_kol.socialdata_bulk import (
    BalanceExhaustedError,
    _httpx_socialdata_get,
)


# ---------------------------------------------------------------------------
# Verdict classification
# ---------------------------------------------------------------------------


VERDICT_ALIVE = "alive"
VERDICT_NOT_FOUND = "not_found"
VERDICT_SUSPENDED = "suspended"
VERDICT_ERROR = "error"


@dataclass(slots=True)
class CheckResult:
    candidate_id: int
    handle: str
    verdict: str
    detail: str = ""


def classify(handle: str) -> CheckResult:
    """Hit SocialData for one handle, classify the response."""
    try:
        data = _httpx_socialdata_get(f"/twitter/user/{handle}")
    except BalanceExhaustedError:
        # Re-raise so the caller can abort the batch — no point burning
        # more retries if the account is dry.
        raise
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code if exc.response is not None else 0
        if code in (404, 410):
            return CheckResult(
                candidate_id=0,
                handle=handle,
                verdict=VERDICT_NOT_FOUND,
                detail=f"SocialData {code}",
            )
        return CheckResult(
            candidate_id=0,
            handle=handle,
            verdict=VERDICT_ERROR,
            detail=f"HTTP {code} after retries",
        )
    except (httpx.HTTPError, RuntimeError) as exc:
        return CheckResult(
            candidate_id=0,
            handle=handle,
            verdict=VERDICT_ERROR,
            detail=str(exc)[:120],
        )

    # SocialData also signals not-found via 200 with status:error
    if isinstance(data, dict) and data.get("status") == "error":
        msg = (data.get("message") or "").lower()
        verdict = VERDICT_SUSPENDED if "suspend" in msg else VERDICT_NOT_FOUND
        return CheckResult(
            candidate_id=0,
            handle=handle,
            verdict=verdict,
            detail=data.get("message", "error"),
        )
    return CheckResult(
        candidate_id=0,
        handle=handle,
        verdict=VERDICT_ALIVE,
    )


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


FILTER_QUERIES = {
    "risky": (
        # Highest-risk: no bio + no twitter_id. These rows have neither
        # signal that a real account exists. Likely to be hallucinations
        # that slipped through.
        "SELECT candidate_id, handle_normalized "
        "FROM kol_candidates "
        "WHERE is_unresolved = 0 AND status = 'active' "
        "  AND (bio_snapshot IS NULL OR bio_snapshot = '') "
        "  AND twitter_id IS NULL "
        "ORDER BY candidate_id"
    ),
    "unverified": (
        # All rows that never went through paid enrichment. Most are
        # legitimate (X-list-parsed rows that never got Grok-enriched);
        # the verification still catches the hallucination subset.
        "SELECT candidate_id, handle_normalized "
        "FROM kol_candidates "
        "WHERE is_unresolved = 0 AND status = 'active' "
        "  AND twitter_id IS NULL "
        "ORDER BY candidate_id"
    ),
    "all": (
        "SELECT candidate_id, handle_normalized "
        "FROM kol_candidates "
        "WHERE is_unresolved = 0 AND status = 'active' "
        "ORDER BY candidate_id"
    ),
}


def select_candidates(conn: Any, filter_name: str, limit: int | None) -> list[tuple[int, str]]:
    sql = FILTER_QUERIES[filter_name]
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql).fetchall()
    return [(r["candidate_id"], r["handle_normalized"]) for r in rows]


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


KO5_SOURCE_PREFIX = "kol_graph:archived_by_ko5:"


def archive_candidate(conn: Any, candidate_id: int, verdict: str, detail: str) -> None:
    """Soft-archive a row + append a discovery_source tag for the audit trail."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    tag = f"{KO5_SOURCE_PREFIX}{today}:{verdict}"

    row = conn.execute(
        "SELECT discovery_sources_json FROM kol_candidates WHERE candidate_id = :cid",
        {"cid": candidate_id},
    ).fetchone()
    if row is None:
        return
    sources = json.loads(row["discovery_sources_json"] or "[]")
    if tag not in sources:
        sources.append(tag)

    conn.execute(
        "UPDATE kol_candidates "
        "SET status = 'archived', discovery_sources_json = :sources "
        "WHERE candidate_id = :cid",
        {"cid": candidate_id, "sources": json.dumps(sources)},
    )

    # Audit log entry — sable_platform.db.audit.log_audit signature is
    # (conn, actor, action, *, org_id, entity_id, detail, source). The
    # entity_id is the candidate_id so a future restore script can find
    # exactly what got archived.
    try:
        from sable_platform.db.audit import log_audit
        log_audit(
            conn,
            actor="ko5_backfill_script",
            action="archive_candidate",
            entity_id=str(candidate_id),
            detail={
                "entity_type": "kol_candidate",
                "verdict": verdict,
                "socialdata_detail": detail[:200],
            },
            source="ko5_backfill",
        )
    except Exception as exc:  # noqa: BLE001 — defensive, audit is best-effort
        print(
            f"  [warn] audit log skipped for cid={candidate_id}: {exc}",
            file=sys.stderr,
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument(
        "--filter",
        choices=list(FILTER_QUERIES.keys()),
        default="risky",
        help="Which candidate subset to verify.",
    )
    p.add_argument("--limit", type=int, default=None, help="Stop after N rows.")
    p.add_argument(
        "--apply",
        action="store_true",
        help="Actually archive 404/suspended rows. Default is dry-run.",
    )
    p.add_argument(
        "--batch-size",
        type=int,
        default=50,
        help="Status-print interval. Doesn't affect correctness.",
    )
    args = p.parse_args()

    with open_db() as conn:
        targets = select_candidates(conn, args.filter, args.limit)
        print(
            f"selected {len(targets)} candidates ({args.filter} filter, "
            f"limit={args.limit or 'none'})"
        )
        if not targets:
            return 0

        est_cost = len(targets) * 0.0002
        print(f"estimated SocialData spend: ~${est_cost:.4f}")
        print(f"mode: {'APPLY (will archive)' if args.apply else 'dry-run'}")
        print()

        counters = {
            VERDICT_ALIVE: 0,
            VERDICT_NOT_FOUND: 0,
            VERDICT_SUSPENDED: 0,
            VERDICT_ERROR: 0,
        }
        to_archive: list[CheckResult] = []
        start = time.time()

        for i, (cid, handle) in enumerate(targets, 1):
            try:
                result = classify(handle)
            except BalanceExhaustedError as e:
                print(f"\n[fatal] SocialData balance exhausted at row {i}: {e}", file=sys.stderr)
                return 2
            result.candidate_id = cid
            counters[result.verdict] += 1
            if result.verdict in (VERDICT_NOT_FOUND, VERDICT_SUSPENDED):
                to_archive.append(result)
                print(f"  {result.verdict:10s} @{handle:30s} cid={cid}  {result.detail}")
            elif result.verdict == VERDICT_ERROR:
                print(f"  ERROR      @{handle:30s} cid={cid}  {result.detail}")

            if i % args.batch_size == 0:
                elapsed = time.time() - start
                rate = i / elapsed if elapsed > 0 else 0
                eta = (len(targets) - i) / rate if rate > 0 else 0
                print(
                    f"  [{i:5d}/{len(targets)}] "
                    f"alive={counters[VERDICT_ALIVE]} "
                    f"not_found={counters[VERDICT_NOT_FOUND]} "
                    f"suspended={counters[VERDICT_SUSPENDED]} "
                    f"error={counters[VERDICT_ERROR]} "
                    f"({rate:.1f}/s, eta {eta:.0f}s)"
                )

        print()
        print("=" * 50)
        print(f"alive       : {counters[VERDICT_ALIVE]}")
        print(f"not_found   : {counters[VERDICT_NOT_FOUND]}")
        print(f"suspended   : {counters[VERDICT_SUSPENDED]}")
        print(f"error       : {counters[VERDICT_ERROR]}")
        print(f"to_archive  : {len(to_archive)}")
        print()

        if args.apply and to_archive:
            print(f"applying archive on {len(to_archive)} rows...")
            for r in to_archive:
                archive_candidate(conn, r.candidate_id, r.verdict, r.detail)
            conn.commit()
            print("done.")
        elif to_archive:
            print(f"dry-run: would archive {len(to_archive)} rows. Pass --apply to write.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
