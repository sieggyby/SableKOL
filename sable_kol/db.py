"""SableKOL bank DB helpers.

Thin wrappers over SablePlatform's ``get_db()`` for the three migration-032
tables: ``kol_candidates``, ``project_profiles_external``, ``kol_handle_resolution_conflicts``.

JSON columns are encoded/decoded transparently. The partial unique index on
``kol_candidates(handle_normalized) WHERE is_unresolved=0`` is honored by
``upsert_candidate`` — if an upsert hits the constraint, the new row is inserted
with ``is_unresolved=1`` and a row is added to ``kol_handle_resolution_conflicts``.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

@contextmanager
def open_db() -> Iterator[Any]:
    """Open a sable.db connection via SablePlatform's get_db().

    Yields a CompatConnection (supports ``?`` and ``:named`` params plus
    ``row["col"]`` access). The connection is closed on exit.
    """
    from sable_platform.db.connection import get_db
    conn = get_db()
    try:
        yield conn
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# kol_candidates
# ---------------------------------------------------------------------------

# Columns that hold JSON. Decoded on read, encoded on write.
_JSON_COLS_CANDIDATE = {
    "handle_history_json",
    "discovery_sources_json",
    "archetype_tags_json",
    "sector_tags_json",
    "sable_relationship_json",
}

# Default sable_relationship structure (matches column server_default).
EMPTY_RELATIONSHIP: dict[str, list] = {"communities": [], "operators": []}


@dataclass
class Candidate:
    candidate_id: int | None = None
    twitter_id: str | None = None
    handle_normalized: str = ""
    is_unresolved: int = 0
    handle_history: list[dict] = field(default_factory=list)
    display_name: str | None = None
    bio_snapshot: str | None = None
    followers_snapshot: int | None = None
    discovery_sources: list[str] = field(default_factory=list)
    first_seen_at: str | None = None
    last_seen_at: str | None = None
    archetype_tags: list[str] = field(default_factory=list)
    sector_tags: list[str] = field(default_factory=list)
    sable_relationship: dict = field(default_factory=lambda: dict(EMPTY_RELATIONSHIP))
    enrichment_tier: str = "none"
    last_enriched_at: str | None = None
    status: str = "active"
    manual_notes: str | None = None
    # Migration 033 columns
    kol_strength_score: float | None = None
    verified: int = 0
    account_created_at: str | None = None
    # Migration 034 columns
    listed_count: int | None = None
    tweets_count: int | None = None
    following_count: int | None = None
    credibility_signal: str | None = None
    real_name_known: int = 0
    notes: str | None = None
    # Migration 035 columns
    location: str | None = None
    # Migration 036: cross-platform presence (Instagram/TikTok/Threads/etc as dict)
    platform_presence: dict = field(default_factory=dict)


def _row_to_candidate(row: Any) -> Candidate:
    keys = set(row.keys()) if hasattr(row, "keys") else set()
    return Candidate(
        candidate_id=row["candidate_id"],
        twitter_id=row["twitter_id"],
        handle_normalized=row["handle_normalized"],
        is_unresolved=row["is_unresolved"],
        handle_history=json.loads(row["handle_history_json"] or "[]"),
        display_name=row["display_name"],
        bio_snapshot=row["bio_snapshot"],
        followers_snapshot=row["followers_snapshot"],
        discovery_sources=json.loads(row["discovery_sources_json"] or "[]"),
        first_seen_at=row["first_seen_at"],
        last_seen_at=row["last_seen_at"],
        archetype_tags=json.loads(row["archetype_tags_json"] or "[]"),
        sector_tags=json.loads(row["sector_tags_json"] or "[]"),
        sable_relationship=json.loads(
            row["sable_relationship_json"]
            or json.dumps(EMPTY_RELATIONSHIP)
        ),
        enrichment_tier=row["enrichment_tier"],
        last_enriched_at=row["last_enriched_at"],
        status=row["status"],
        manual_notes=row["manual_notes"],
        # Migration 033 — present in fresh schemas, defaulted otherwise.
        kol_strength_score=row["kol_strength_score"] if "kol_strength_score" in keys else None,
        verified=row["verified"] if "verified" in keys else 0,
        account_created_at=row["account_created_at"] if "account_created_at" in keys else None,
        # Migration 034
        listed_count=row["listed_count"] if "listed_count" in keys else None,
        tweets_count=row["tweets_count"] if "tweets_count" in keys else None,
        following_count=row["following_count"] if "following_count" in keys else None,
        credibility_signal=row["credibility_signal"] if "credibility_signal" in keys else None,
        real_name_known=row["real_name_known"] if "real_name_known" in keys else 0,
        notes=row["notes"] if "notes" in keys else None,
        # Migration 035
        location=row["location"] if "location" in keys else None,
        # Migration 036
        platform_presence=(
            json.loads(row["platform_presence_json"] or "{}")
            if "platform_presence_json" in keys
            else {}
        ),
    )


def normalize_handle(handle: str) -> str:
    """Lowercase, strip, drop a leading @ if present."""
    h = handle.strip().lower()
    if h.startswith("@"):
        h = h[1:]
    return h


@dataclass
class UpsertResult:
    candidate_id: int
    inserted: bool
    updated: bool
    conflicted: bool  # True if the row was inserted as is_unresolved=1 due to a live duplicate
    conflict_id: int | None = None


def upsert_candidate(
    conn: Any,
    *,
    handle: str,
    display_name: str | None = None,
    bio_snapshot: str | None = None,
    followers_snapshot: int | None = None,
    discovery_source: str,
    twitter_id: str | None = None,
) -> UpsertResult:
    """Insert or update a kol_candidates row keyed by normalized handle.

    Behavior:
      * If no live row exists for the handle, INSERT a new live row.
      * If a live row exists and the same ``twitter_id`` (when known) matches,
        UPDATE last_seen_at + append discovery_source.
      * If a live row exists with a *different* known ``twitter_id``, INSERT a new
        is_unresolved=1 row and write a kol_handle_resolution_conflicts entry.
      * If the live row's twitter_id is NULL and the incoming twitter_id is NULL,
        treat as same-row UPDATE (no way to distinguish; operator will resolve later
        if a paid enrichment shows otherwise).
    """
    h = normalize_handle(handle)
    existing_live = conn.execute(
        "SELECT * FROM kol_candidates "
        "WHERE handle_normalized = :h AND is_unresolved = 0",
        {"h": h},
    ).fetchone()

    if existing_live is None:
        # Fresh live insert.
        candidate_id = _insert_candidate(
            conn,
            handle_normalized=h,
            is_unresolved=0,
            twitter_id=twitter_id,
            display_name=display_name,
            bio_snapshot=bio_snapshot,
            followers_snapshot=followers_snapshot,
            discovery_sources=[discovery_source],
        )
        return UpsertResult(candidate_id=candidate_id, inserted=True, updated=False, conflicted=False)

    # Live row exists.
    existing_tid = existing_live["twitter_id"]
    is_same_row = (
        twitter_id is None
        or existing_tid is None
        or existing_tid == twitter_id
    )

    if is_same_row:
        # UPDATE: bump last_seen_at, append discovery_source if new, fill blanks.
        sources = json.loads(existing_live["discovery_sources_json"] or "[]")
        if discovery_source not in sources:
            sources.append(discovery_source)
        conn.execute(
            "UPDATE kol_candidates SET "
            "  last_seen_at = CURRENT_TIMESTAMP, "
            "  discovery_sources_json = :sources, "
            "  display_name = COALESCE(:display_name, display_name), "
            "  bio_snapshot = COALESCE(:bio, bio_snapshot), "
            "  followers_snapshot = COALESCE(:fc, followers_snapshot), "
            "  twitter_id = COALESCE(twitter_id, :tid) "
            "WHERE candidate_id = :cid",
            {
                "sources": json.dumps(sources),
                "display_name": display_name,
                "bio": bio_snapshot,
                "fc": followers_snapshot,
                "tid": twitter_id,
                "cid": existing_live["candidate_id"],
            },
        )
        conn.commit()
        return UpsertResult(
            candidate_id=existing_live["candidate_id"],
            inserted=False,
            updated=True,
            conflicted=False,
        )

    # Conflict: live row's twitter_id differs from incoming. Add as unresolved.
    new_id = _insert_candidate(
        conn,
        handle_normalized=h,
        is_unresolved=1,
        twitter_id=twitter_id,
        display_name=display_name,
        bio_snapshot=bio_snapshot,
        followers_snapshot=followers_snapshot,
        discovery_sources=[discovery_source],
    )
    cur = conn.execute(
        "INSERT INTO kol_handle_resolution_conflicts "
        "  (incoming_candidate_id, existing_candidate_id, resolved_twitter_id, resolution_state) "
        "VALUES (:in_id, :ex_id, :tid, 'open')",
        {
            "in_id": new_id,
            "ex_id": existing_live["candidate_id"],
            "tid": twitter_id,
        },
    )
    conn.commit()
    conflict_id = (
        cur.lastrowid
        if hasattr(cur, "lastrowid") and cur.lastrowid
        else conn.execute(
            "SELECT MAX(conflict_id) AS m FROM kol_handle_resolution_conflicts"
        ).fetchone()["m"]
    )
    return UpsertResult(
        candidate_id=new_id,
        inserted=True,
        updated=False,
        conflicted=True,
        conflict_id=conflict_id,
    )


def _insert_candidate(
    conn: Any,
    *,
    handle_normalized: str,
    is_unresolved: int,
    twitter_id: str | None,
    display_name: str | None,
    bio_snapshot: str | None,
    followers_snapshot: int | None,
    discovery_sources: list[str],
) -> int:
    cur = conn.execute(
        "INSERT INTO kol_candidates "
        "  (handle_normalized, is_unresolved, twitter_id, display_name, bio_snapshot, "
        "   followers_snapshot, discovery_sources_json) "
        "VALUES (:h, :is_un, :tid, :dn, :bio, :fc, :sources)",
        {
            "h": handle_normalized,
            "is_un": is_unresolved,
            "tid": twitter_id,
            "dn": display_name,
            "bio": bio_snapshot,
            "fc": followers_snapshot,
            "sources": json.dumps(discovery_sources),
        },
    )
    conn.commit()
    if hasattr(cur, "lastrowid") and cur.lastrowid:
        return cur.lastrowid
    # Fallback for SA-wrapped connections.
    row = conn.execute(
        "SELECT candidate_id FROM kol_candidates "
        "WHERE handle_normalized = :h "
        "ORDER BY candidate_id DESC LIMIT 1",
        {"h": handle_normalized},
    ).fetchone()
    return row["candidate_id"]


def get_candidate_by_handle(conn: Any, handle: str) -> Candidate | None:
    """Fetch the LIVE row for a normalized handle (is_unresolved=0)."""
    h = normalize_handle(handle)
    row = conn.execute(
        "SELECT * FROM kol_candidates "
        "WHERE handle_normalized = :h AND is_unresolved = 0",
        {"h": h},
    ).fetchone()
    return _row_to_candidate(row) if row else None


def list_candidates(
    conn: Any,
    *,
    status: str | None = "active",
    only_classified: bool = False,
    limit: int | None = None,
) -> list[Candidate]:
    """List live candidates with simple filters.

    ``only_classified=True`` filters to rows with non-empty archetype_tags_json.
    """
    where = ["is_unresolved = 0"]
    params: dict[str, Any] = {}
    if status is not None:
        where.append("status = :status")
        params["status"] = status
    if only_classified:
        where.append("archetype_tags_json != '[]'")
    sql = "SELECT * FROM kol_candidates WHERE " + " AND ".join(where)
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql, params).fetchall()
    return [_row_to_candidate(r) for r in rows]


def list_unclassified(conn: Any, limit: int | None = None) -> list[Candidate]:
    """Live rows that haven't been Haiku-classified yet."""
    sql = (
        "SELECT * FROM kol_candidates "
        "WHERE is_unresolved = 0 AND archetype_tags_json = '[]'"
    )
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql, {}).fetchall()
    return [_row_to_candidate(r) for r in rows]


def update_classification(
    conn: Any,
    *,
    candidate_id: int,
    archetype_tags: list[str],
    sector_tags: list[str],
    status: str,
) -> None:
    conn.execute(
        "UPDATE kol_candidates SET "
        "  archetype_tags_json = :a, sector_tags_json = :s, status = :st "
        "WHERE candidate_id = :cid",
        {
            "a": json.dumps(archetype_tags),
            "s": json.dumps(sector_tags),
            "st": status,
            "cid": candidate_id,
        },
    )
    conn.commit()


def update_relationship(
    conn: Any,
    *,
    candidate_id: int,
    relationship: dict,
    extra_discovery_source: str | None = None,
) -> None:
    """Replace sable_relationship_json. Optionally append a discovery_source."""
    if extra_discovery_source is not None:
        existing = conn.execute(
            "SELECT discovery_sources_json FROM kol_candidates WHERE candidate_id = :cid",
            {"cid": candidate_id},
        ).fetchone()
        sources = json.loads(existing["discovery_sources_json"] or "[]")
        if extra_discovery_source not in sources:
            sources.append(extra_discovery_source)
        conn.execute(
            "UPDATE kol_candidates SET "
            "  sable_relationship_json = :r, discovery_sources_json = :sources "
            "WHERE candidate_id = :cid",
            {
                "r": json.dumps(relationship),
                "sources": json.dumps(sources),
                "cid": candidate_id,
            },
        )
    else:
        conn.execute(
            "UPDATE kol_candidates SET sable_relationship_json = :r WHERE candidate_id = :cid",
            {"r": json.dumps(relationship), "cid": candidate_id},
        )
    conn.commit()


# ---------------------------------------------------------------------------
# project_profiles_external
# ---------------------------------------------------------------------------

@dataclass
class ExternalProfile:
    handle_normalized: str
    twitter_id: str | None
    sector_tags: list[str]
    themes: list[str]
    profile_blob: str | None
    enrichment_source: str
    last_enriched_at: str | None
    created_at: str | None
    last_used_at: str | None


def get_external_profile(conn: Any, handle: str) -> ExternalProfile | None:
    h = normalize_handle(handle)
    row = conn.execute(
        "SELECT * FROM project_profiles_external WHERE handle_normalized = :h",
        {"h": h},
    ).fetchone()
    if not row:
        return None
    return ExternalProfile(
        handle_normalized=row["handle_normalized"],
        twitter_id=row["twitter_id"],
        sector_tags=json.loads(row["sector_tags_json"] or "[]"),
        themes=json.loads(row["themes_json"] or "[]"),
        profile_blob=row["profile_blob"],
        enrichment_source=row["enrichment_source"],
        last_enriched_at=row["last_enriched_at"],
        created_at=row["created_at"],
        last_used_at=row["last_used_at"],
    )


def upsert_external_profile(
    conn: Any,
    *,
    handle: str,
    sector_tags: list[str],
    themes: list[str],
    profile_blob: str | None,
    enrichment_source: str,
    twitter_id: str | None = None,
    mark_enriched_now: bool = False,
) -> None:
    h = normalize_handle(handle)
    existing = conn.execute(
        "SELECT 1 AS x FROM project_profiles_external WHERE handle_normalized = :h",
        {"h": h},
    ).fetchone()
    enriched_clause = "CURRENT_TIMESTAMP" if mark_enriched_now else "NULL"
    if existing:
        conn.execute(
            f"UPDATE project_profiles_external SET "
            f"  twitter_id = COALESCE(:tid, twitter_id), "
            f"  sector_tags_json = :s, themes_json = :t, "
            f"  profile_blob = COALESCE(:pb, profile_blob), "
            f"  enrichment_source = :src, "
            f"  last_enriched_at = COALESCE({enriched_clause}, last_enriched_at), "
            f"  last_used_at = CURRENT_TIMESTAMP "
            f"WHERE handle_normalized = :h",
            {
                "tid": twitter_id,
                "s": json.dumps(sector_tags),
                "t": json.dumps(themes),
                "pb": profile_blob,
                "src": enrichment_source,
                "h": h,
            },
        )
    else:
        conn.execute(
            f"INSERT INTO project_profiles_external "
            f"  (handle_normalized, twitter_id, sector_tags_json, themes_json, "
            f"   profile_blob, enrichment_source, last_enriched_at) "
            f"VALUES (:h, :tid, :s, :t, :pb, :src, {enriched_clause})",
            {
                "h": h,
                "tid": twitter_id,
                "s": json.dumps(sector_tags),
                "t": json.dumps(themes),
                "pb": profile_blob,
                "src": enrichment_source,
            },
        )
    conn.commit()


def mark_external_profile_used(conn: Any, handle: str) -> None:
    h = normalize_handle(handle)
    conn.execute(
        "UPDATE project_profiles_external SET last_used_at = CURRENT_TIMESTAMP "
        "WHERE handle_normalized = :h",
        {"h": h},
    )
    conn.commit()


# ---------------------------------------------------------------------------
# kol_handle_resolution_conflicts
# ---------------------------------------------------------------------------

def list_open_conflicts(conn: Any) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM kol_handle_resolution_conflicts WHERE resolution_state = 'open' "
        "ORDER BY detected_at"
    ).fetchall()
    return [dict(r) for r in rows]


def update_conflict_state(
    conn: Any,
    *,
    conflict_id: int,
    state: str,
    notes: str | None = None,
) -> None:
    conn.execute(
        "UPDATE kol_handle_resolution_conflicts SET "
        "  resolution_state = :state, resolved_at = CURRENT_TIMESTAMP, "
        "  notes = COALESCE(:notes, notes) "
        "WHERE conflict_id = :cid",
        {"state": state, "notes": notes, "cid": conflict_id},
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Bank stats (used by `sable-kol bank stats`)
# ---------------------------------------------------------------------------

def bank_stats(conn: Any) -> dict:
    total = conn.execute(
        "SELECT COUNT(*) AS n FROM kol_candidates WHERE is_unresolved = 0"
    ).fetchone()["n"]
    by_status_rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM kol_candidates "
        "WHERE is_unresolved = 0 GROUP BY status ORDER BY n DESC"
    ).fetchall()
    classified = conn.execute(
        "SELECT COUNT(*) AS n FROM kol_candidates "
        "WHERE is_unresolved = 0 AND archetype_tags_json != '[]'"
    ).fetchone()["n"]
    open_conflicts = conn.execute(
        "SELECT COUNT(*) AS n FROM kol_handle_resolution_conflicts "
        "WHERE resolution_state = 'open'"
    ).fetchone()["n"]
    unresolved = conn.execute(
        "SELECT COUNT(*) AS n FROM kol_candidates WHERE is_unresolved = 1"
    ).fetchone()["n"]
    return {
        "total_live": total,
        "by_status": {r["status"]: r["n"] for r in by_status_rows},
        "classified": classified,
        "unclassified": total - classified,
        "open_conflicts": open_conflicts,
        "unresolved_rows": unresolved,
    }
