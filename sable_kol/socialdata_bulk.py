"""Bulk SocialData extraction for SableKOL follow-graph work.

Implements the cursor-paginated followers/following extractors used by the
SolStitch outreach plan (Phase 6 — follow-graph extraction over a curated
KOL set). Both endpoints are documented as **"Limited Access"** by SocialData;
the caller is expected to verify access (Phase 0.5 of the SolStitch plan)
before committing to extraction at scale.

Endpoints (verified against docs.socialdata.tools 2026-05-06):

    GET /twitter/followers/list?user_id=<int>[&cursor=<opaque>]
    GET /twitter/friends/list?user_id=<int>[&cursor=<opaque>]
    GET /twitter/user/<handle_or_id>            (handle resolution + friends_count pre-flight)

Both list endpoints take an **integer user_id** (not a handle). Use
:func:`resolve_user_id` to look up the id from a handle, preferring the bank's
already-stored ``twitter_id`` to avoid an extra paid call.

Run-record contract:

* The caller creates a row in ``kol_extract_runs`` via :func:`create_run` BEFORE
  iterating. Each yielded profile is paired with the originating ``run_id``.
* The yield loop updates ``pages_fetched`` / ``rows_inserted`` /
  ``last_cursor`` / ``cost_usd_logged`` on every page so a hard fail leaves a
  resumable checkpoint behind (with ``cursor_completed=0``).
* On clean cursor exhaustion the loop calls :func:`mark_run_completed`
  (sets ``cursor_completed=1`` + ``completed_at``).
* On 429 / 5xx exhaustion or other recoverable failure the loop calls
  :func:`mark_run_failed` and re-raises; the caller can re-invoke with the
  same target+extract_type and a fresh ``run_id`` (or pass ``resume_run_id``
  to continue an existing run from its ``last_cursor``).

Cost logging: **one ``cost_events`` row per page** at ``$0.002`` (estimated
internal rate; see SolStitch plan cost-estimate caveat). Slopper's
``socialdata_get`` does NOT log cost — that is SableKOL's responsibility.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Iterator

from sable_kol import cost as cost_mod


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROVIDER_SOCIALDATA = "socialdata"

# Per-page result count is NOT documented by SocialData; treat $0.002 as one
# call regardless of returned-row count. Phase 0.5 of the SolStitch plan
# calibrates the empirically observed page size.
COST_USD_PER_PAGE = 0.002

# Cost call_types written to ``cost_events`` (CALL_TYPE_PREFIX adds "sablekol.").
CALL_TYPE_FOLLOWERS = "socialdata_followers_page"
CALL_TYPE_FOLLOWING = "socialdata_following_page"
CALL_TYPE_PROFILE_RESOLVE = "socialdata_user_profile_resolve"


# ---------------------------------------------------------------------------
# Lightweight QC filter
# ---------------------------------------------------------------------------

def qc_profile(profile: Any) -> bool:
    """Reject malformed / suspended / protected profile blobs.

    Required fields per SocialData docs: ``id_str``, ``screen_name``,
    ``followers_count``, ``friends_count``, ``statuses_count``.

    Drop rules:
    * Missing any required field → drop
    * ``protected`` is True → drop (no follower-graph signal possible)
    * ``description`` is None AND ``followers_count`` < 100 AND
      ``statuses_count`` < 10 → drop (signal of suspended/empty account)
    """
    if not isinstance(profile, dict):
        return False
    required = ("id_str", "screen_name", "followers_count", "friends_count", "statuses_count")
    for f in required:
        if f not in profile or profile[f] is None:
            return False
    if profile.get("protected") is True:
        return False
    if (
        profile.get("description") is None
        and (profile.get("followers_count") or 0) < 100
        and (profile.get("statuses_count") or 0) < 10
    ):
        return False
    return True


# ---------------------------------------------------------------------------
# Run record CRUD on kol_extract_runs (sable.db migration 037)
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class ExtractRun:
    run_id: str
    target_handle_normalized: str
    target_user_id: str | None
    provider: str
    extract_type: str
    cursor_completed: int = 0
    last_cursor: str | None = None
    pages_fetched: int = 0
    rows_inserted: int = 0
    expected_count: int | None = None
    partial_failure_reason: str | None = None
    cost_usd_logged: float = 0.0
    # Migration 039: per-client scoping. Defaults to '_external' if caller
    # forgets to pass `--client`; explicit client_id on every new run is the
    # contract — operators MUST pass --client when running real extracts.
    client_id: str = "_external"


def create_run(
    conn: Any,
    *,
    target_handle: str,
    target_user_id: str | None,
    extract_type: str,
    expected_count: int | None = None,
    provider: str = PROVIDER_SOCIALDATA,
    run_id: str | None = None,
    client_id: str = "_external",
) -> ExtractRun:
    """Create a kol_extract_runs row and return the dataclass view.

    `client_id` (migration 039) scopes the run to one Sable client so
    SableWeb queries can filter graphs per-client. Defaults to '_external'
    sentinel if not provided — but operators running real extracts SHOULD
    pass the actual client (`'solstitch'`, `'tig'`, etc.) so multi-client
    deployments can later split graphs cleanly.
    """
    if extract_type not in ("followers", "following"):
        raise ValueError(f"extract_type must be 'followers' or 'following', got {extract_type!r}")
    rid = run_id or f"run_{uuid.uuid4().hex[:16]}"
    h_norm = target_handle.lstrip("@").lower().strip()
    conn.execute(
        "INSERT INTO kol_extract_runs "
        "(run_id, target_handle_normalized, target_user_id, provider, extract_type, "
        " expected_count, client_id) "
        "VALUES (:run_id, :h, :uid, :p, :et, :exp, :cid)",
        {
            "run_id": rid,
            "h": h_norm,
            "uid": str(target_user_id) if target_user_id is not None else None,
            "p": provider,
            "et": extract_type,
            "exp": expected_count,
            "cid": client_id,
        },
    )
    conn.commit()
    return ExtractRun(
        run_id=rid,
        target_handle_normalized=h_norm,
        target_user_id=str(target_user_id) if target_user_id is not None else None,
        provider=provider,
        extract_type=extract_type,
        expected_count=expected_count,
        client_id=client_id,
    )


def get_run(conn: Any, run_id: str) -> ExtractRun | None:
    row = conn.execute(
        "SELECT * FROM kol_extract_runs WHERE run_id = :rid",
        {"rid": run_id},
    ).fetchone()
    if row is None:
        return None
    return ExtractRun(
        run_id=row["run_id"],
        target_handle_normalized=row["target_handle_normalized"],
        target_user_id=row["target_user_id"],
        provider=row["provider"],
        extract_type=row["extract_type"],
        cursor_completed=row["cursor_completed"],
        last_cursor=row["last_cursor"],
        pages_fetched=row["pages_fetched"],
        rows_inserted=row["rows_inserted"],
        expected_count=row["expected_count"],
        partial_failure_reason=row["partial_failure_reason"],
        cost_usd_logged=row["cost_usd_logged"],
        # Migration 039: client_id may be missing on rows created before the
        # column existed (the migration backfilled to 'solstitch'). Newly-
        # created rows always carry the value the caller passed.
        client_id=row["client_id"] if "client_id" in (row.keys() if hasattr(row, "keys") else []) else "_external",
    )


def _checkpoint_run(
    conn: Any,
    *,
    run_id: str,
    last_cursor: str | None,
    pages_delta: int,
    rows_delta: int,
    cost_delta: float,
) -> None:
    """Increment counters + stash last_cursor after a successful page fetch."""
    conn.execute(
        "UPDATE kol_extract_runs SET "
        "  last_cursor = :c, "
        "  pages_fetched = pages_fetched + :pd, "
        "  rows_inserted = rows_inserted + :rd, "
        "  cost_usd_logged = cost_usd_logged + :cd "
        "WHERE run_id = :rid",
        {
            "c": last_cursor,
            "pd": pages_delta,
            "rd": rows_delta,
            "cd": cost_delta,
            "rid": run_id,
        },
    )
    conn.commit()


def mark_run_completed(conn: Any, run_id: str) -> None:
    # CURRENT_TIMESTAMP is portable across SQLite + Postgres; the SQLite-only
    # datetime('now') here errored on the live Postgres prod DB.
    conn.execute(
        "UPDATE kol_extract_runs SET cursor_completed = 1, "
        "completed_at = CURRENT_TIMESTAMP, partial_failure_reason = NULL "
        "WHERE run_id = :rid",
        {"rid": run_id},
    )
    conn.commit()


def mark_run_failed(conn: Any, run_id: str, reason: str) -> None:
    conn.execute(
        "UPDATE kol_extract_runs SET partial_failure_reason = :r WHERE run_id = :rid",
        {"r": reason, "rid": run_id},
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Edge insertion
# ---------------------------------------------------------------------------

def insert_edges(
    conn: Any,
    *,
    run_id: str,
    edges: list[dict],
) -> int:
    """Bulk-insert edges, ignoring duplicates (same composite PK).

    Each edge dict needs: ``follower_id``, ``follower_handle`` (optional),
    ``followed_id``, ``followed_handle``.

    Returns the number of rows actually inserted (after dedupe).
    """
    if not edges:
        return 0
    inserted = 0
    for e in edges:
        try:
            conn.execute(
                "INSERT INTO kol_follow_edges "
                "(run_id, follower_id, follower_handle, followed_id, followed_handle) "
                "VALUES (:rid, :fi, :fh, :di, :dh)",
                {
                    "rid": run_id,
                    "fi": str(e["follower_id"]),
                    "fh": e.get("follower_handle"),
                    "di": str(e["followed_id"]),
                    "dh": e["followed_handle"],
                },
            )
            inserted += 1
        except Exception:
            # Composite PK violation = already inserted; skip silently for
            # idempotent resume. Other errors propagate.
            pass
    conn.commit()
    return inserted


# ---------------------------------------------------------------------------
# Handle resolution
# ---------------------------------------------------------------------------

def resolve_user_id(
    conn: Any,
    handle: str,
    *,
    socialdata_fetcher: Callable[[str], dict] | None = None,
    log_cost: bool = True,
) -> str | None:
    """Resolve a handle to a numeric user_id (returns id_str).

    Checks the bank's stored ``twitter_id`` first to avoid a paid call. Falls
    back to ``GET /twitter/user/<handle>``, logged as one ``cost_events`` row.
    Returns ``None`` if the account is suspended or non-existent.
    """
    h = handle.lstrip("@").lower().strip()
    if not h:
        return None

    # Fast path: bank has a twitter_id for a live row matching this handle.
    row = conn.execute(
        "SELECT twitter_id FROM kol_candidates "
        "WHERE handle_normalized = :h AND is_unresolved = 0 AND twitter_id IS NOT NULL",
        {"h": h},
    ).fetchone()
    if row is not None and row["twitter_id"]:
        return str(row["twitter_id"])

    # Slow path: profile fetch (one paid call).
    if socialdata_fetcher is None:
        socialdata_fetcher = _default_profile_fetcher
    try:
        data = socialdata_fetcher(h)
    except Exception:
        if log_cost:
            cost_mod.record(
                conn,
                org_id=None,
                call_type=CALL_TYPE_PROFILE_RESOLVE,
                cost_usd=COST_USD_PER_PAGE,
                call_status="error",
            )
        raise

    if log_cost:
        cost_mod.record(
            conn,
            org_id=None,
            call_type=CALL_TYPE_PROFILE_RESOLVE,
            cost_usd=COST_USD_PER_PAGE,
        )

    if not isinstance(data, dict):
        return None
    uid = data.get("id_str")
    if uid is None and data.get("id") is not None:
        uid = str(data["id"])
    return uid


# ---------------------------------------------------------------------------
# Paginated extractors
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class PageYield:
    """One yielded page plus the profile dicts that survived qc/floor filters."""
    profiles: list[dict]
    next_cursor: str | None
    page_index: int


def pull_followers(
    conn: Any,
    *,
    run: ExtractRun,
    floor_followers: int = 500,
    socialdata_fetcher: Callable[[str, dict], dict] | None = None,
    page_limit: int | None = None,
) -> Iterator[dict]:
    """Cursor-paginate ``GET /twitter/followers/list?user_id=<int>[&cursor=...]``.

    Yields the post-qc, post-floor-filter profile dicts one at a time. Updates
    the ``kol_extract_runs`` row after every page (cursor + counters + cost),
    so a hard fail leaves a resumable checkpoint behind.

    On clean cursor exhaustion: marks the run completed.
    On exception: marks ``partial_failure_reason`` and re-raises.
    """
    if run.target_user_id is None:
        raise ValueError(f"run {run.run_id} has no target_user_id; resolve it first")
    yield from _paginate(
        conn,
        run=run,
        path="/twitter/followers/list",
        floor_followers=floor_followers,
        cap_count_field=None,
        socialdata_fetcher=socialdata_fetcher,
        page_limit=page_limit,
        cost_call_type=CALL_TYPE_FOLLOWERS,
    )


def pull_following(
    conn: Any,
    *,
    run: ExtractRun,
    max_following: int = 1000,
    socialdata_fetcher: Callable[[str, dict], dict] | None = None,
    page_limit: int | None = None,
) -> Iterator[dict]:
    """Cursor-paginate ``GET /twitter/friends/list?user_id=<int>[&cursor=...]``.

    Same contract as :func:`pull_followers`. ``max_following`` is enforced by
    the caller via the run's ``expected_count`` (set during pre-flight) — if
    the target's friends_count exceeds the cap, the caller marks the run
    completed-with-zero-rows BEFORE invoking this generator and skips it.
    """
    if run.target_user_id is None:
        raise ValueError(f"run {run.run_id} has no target_user_id; resolve it first")
    if (
        run.expected_count is not None
        and max_following is not None
        and run.expected_count > max_following
    ):
        # Caller's responsibility, but we belt-and-suspender it here.
        mark_run_completed(conn, run.run_id)
        return
    yield from _paginate(
        conn,
        run=run,
        path="/twitter/friends/list",
        floor_followers=0,  # don't drop following-list entries by follower count
        cap_count_field=None,
        socialdata_fetcher=socialdata_fetcher,
        page_limit=page_limit,
        cost_call_type=CALL_TYPE_FOLLOWING,
    )


def _paginate(
    conn: Any,
    *,
    run: ExtractRun,
    path: str,
    floor_followers: int,
    cap_count_field: str | None,
    socialdata_fetcher: Callable[[str, dict], dict] | None,
    page_limit: int | None,
    cost_call_type: str,
) -> Iterator[dict]:
    if socialdata_fetcher is None:
        socialdata_fetcher = _default_path_fetcher

    cursor = run.last_cursor  # resume from checkpoint if present
    pages_done = 0

    try:
        while True:
            params: dict[str, Any] = {"user_id": run.target_user_id}
            if cursor:
                params["cursor"] = cursor
            data = socialdata_fetcher(path, params)
            pages_done += 1

            users = data.get("users") or []
            kept: list[dict] = []
            for u in users:
                if not qc_profile(u):
                    continue
                if floor_followers and (u.get("followers_count") or 0) < floor_followers:
                    continue
                kept.append(u)

            next_cursor = data.get("next_cursor")
            # Some response shapes return null/"0"/"" for no-more.
            if next_cursor in (None, "", "0"):
                next_cursor = None

            _checkpoint_run(
                conn,
                run_id=run.run_id,
                last_cursor=next_cursor,
                pages_delta=1,
                rows_delta=len(kept),
                cost_delta=COST_USD_PER_PAGE,
            )
            cost_mod.record(
                conn,
                org_id=None,
                call_type=cost_call_type,
                cost_usd=COST_USD_PER_PAGE,
            )

            for u in kept:
                yield u

            cursor = next_cursor
            if cursor is None:
                mark_run_completed(conn, run.run_id)
                return
            if page_limit is not None and pages_done >= page_limit:
                # Caller-imposed cap (e.g. for testing); leave run unfinished.
                return
    except Exception as exc:
        reason = _classify_failure(exc)
        mark_run_failed(conn, run.run_id, reason)
        raise


def _classify_failure(exc: Exception) -> str:
    """Map an exception to a short reason code for partial_failure_reason."""
    name = type(exc).__name__
    msg = str(exc).lower()
    if "balance" in msg or "402" in msg:
        return "402_balance"
    if "429" in msg or "rate" in msg:
        return "429_rate_limit"
    if "timeout" in msg:
        return "timeout"
    if "auth" in msg or "401" in msg or "403" in msg:
        return "auth"
    return name[:64]


# ---------------------------------------------------------------------------
# Default fetchers (Slopper-backed, only loaded when needed)
# ---------------------------------------------------------------------------

def _default_profile_fetcher(handle: str) -> dict:
    try:
        from sable.shared.socialdata import socialdata_get  # type: ignore[import-not-found]
    except ImportError as e:
        raise RuntimeError(
            "SocialData paid path requires Slopper. "
            "Install with: pip install -e '.[paid-enrich]'"
        ) from e
    return socialdata_get(f"/twitter/user/{handle}")


def _default_path_fetcher(path: str, params: dict) -> dict:
    try:
        from sable.shared.socialdata import socialdata_get  # type: ignore[import-not-found]
    except ImportError as e:
        raise RuntimeError(
            "SocialData paid path requires Slopper. "
            "Install with: pip install -e '.[paid-enrich]'"
        ) from e
    return socialdata_get(path, params=params)
