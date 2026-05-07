"""ETL Stage 4 — join the bank against sable.db entities.

Two passes:

1. **Match pass.** For each live ``kol_candidates`` row, look up its handle in
   ``entity_handles`` (platform='twitter'). For every match, fetch the entity's
   home org and current tags (``entity_tags.is_current=1``). Populate
   ``sable_relationship_json.communities`` with one entry per matched org.
   Also append ``"org:<org_id>"`` to ``discovery_sources_json`` if not already there.

2. **Tier-2 fold-in.** Find sable.db entities that look like KOL candidates but
   aren't yet in ``kol_candidates``: entities with the ``voice`` tag (``is_current=1``),
   OR entities with ``status='confirmed'``. Insert each missing entity as a new
   live ``kol_candidates`` row keyed by their primary Twitter handle (if any),
   tagged with ``"sable_db_voice"`` or ``"sable_db_confirmed"`` discovery source.

NOTE: Cross-org "appears in 2+ orgs" cannot be computed from ``entity_handles``
because of its ``UniqueConstraint(platform, handle)`` — each handle maps to at
most one entity. Phase 2 will compute cross-sector signal from ``meta.db``
mention activity instead.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from sable_kol.db import (
    open_db,
    update_relationship,
    upsert_candidate,
)


@dataclass
class CrossrefSummary:
    matched: int = 0
    tier2_added: int = 0
    relationships_written: int = 0


# ---------------------------------------------------------------------------
# Match pass — populate sable_relationship_json on existing bank rows
# ---------------------------------------------------------------------------

def _entities_for_handle(conn, handle_normalized: str) -> list[dict]:
    """Return [{entity_id, org_id, status, tags: [str, ...]}, ...] for matches."""
    rows = conn.execute(
        "SELECT eh.entity_id AS entity_id, e.org_id AS org_id, e.status AS status "
        "FROM entity_handles eh JOIN entities e ON e.entity_id = eh.entity_id "
        "WHERE eh.platform = 'twitter' AND lower(eh.handle) = :h",
        {"h": handle_normalized},
    ).fetchall()
    out = []
    for r in rows:
        tags = [
            t["tag"]
            for t in conn.execute(
                "SELECT tag FROM entity_tags WHERE entity_id = :eid AND is_current = 1",
                {"eid": r["entity_id"]},
            ).fetchall()
        ]
        out.append({
            "entity_id": r["entity_id"],
            "org_id": r["org_id"],
            "status": r["status"],
            "tags": tags,
        })
    return out


def _match_pass(conn) -> tuple[int, int]:
    """Returns (matched_candidates, relationships_written)."""
    rows = conn.execute(
        "SELECT candidate_id, handle_normalized, sable_relationship_json, "
        "       discovery_sources_json "
        "FROM kol_candidates WHERE is_unresolved = 0"
    ).fetchall()
    matched = 0
    written = 0
    for row in rows:
        matches = _entities_for_handle(conn, row["handle_normalized"])
        if not matches:
            continue
        existing_rel = json.loads(
            row["sable_relationship_json"] or '{"communities":[],"operators":[]}'
        )
        existing_sources = json.loads(row["discovery_sources_json"] or "[]")

        # Build communities[] from matches; preserve any operators[] already set.
        communities = []
        for m in matches:
            communities.append({
                "org_id": m["org_id"],
                "tags": m["tags"],
                "entity_status": m["status"],
            })
            tag = f"org:{m['org_id']}"
            if tag not in existing_sources:
                existing_sources.append(tag)
        existing_rel["communities"] = communities

        update_relationship(
            conn,
            candidate_id=row["candidate_id"],
            relationship=existing_rel,
        )
        # update_relationship doesn't take discovery_sources arg without one;
        # write sources separately.
        conn.execute(
            "UPDATE kol_candidates SET discovery_sources_json = :s WHERE candidate_id = :cid",
            {"s": json.dumps(existing_sources), "cid": row["candidate_id"]},
        )
        conn.commit()
        matched += 1
        written += 1
    return matched, written


# ---------------------------------------------------------------------------
# Tier-2 fold-in — add Sable-internal "voices" not yet in the bank
# ---------------------------------------------------------------------------

def _tier2_candidates(conn) -> list[dict]:
    """Sable entities that should be in the bank but aren't.

    Source criterion: entity_tags.tag='voice' AND is_current=1, OR
    entities.status='confirmed'. Only returns those with a primary Twitter handle.
    """
    rows = conn.execute(
        "SELECT DISTINCT e.entity_id AS entity_id, e.org_id AS org_id, "
        "       e.display_name AS display_name, e.status AS status, "
        "       eh.handle AS handle "
        "FROM entities e "
        "JOIN entity_handles eh ON eh.entity_id = e.entity_id "
        "LEFT JOIN entity_tags t "
        "       ON t.entity_id = e.entity_id AND t.tag = 'voice' AND t.is_current = 1 "
        "WHERE eh.platform = 'twitter' "
        "  AND (t.tag IS NOT NULL OR e.status = 'confirmed')"
    ).fetchall()
    return [dict(r) for r in rows]


def _tier2_pass(conn) -> int:
    """Insert any sable.db Tier-2 entities not yet in the bank. Returns # added."""
    added = 0
    for ent in _tier2_candidates(conn):
        handle = (ent["handle"] or "").strip()
        if not handle:
            continue
        # Discovery source label distinguishes voice vs confirmed.
        is_voice = bool(conn.execute(
            "SELECT 1 AS x FROM entity_tags "
            "WHERE entity_id = :eid AND tag = 'voice' AND is_current = 1",
            {"eid": ent["entity_id"]},
        ).fetchone())
        source = "sable_db_voice" if is_voice else "sable_db_confirmed"

        # Check if already in bank as a live row.
        already = conn.execute(
            "SELECT candidate_id FROM kol_candidates "
            "WHERE handle_normalized = lower(:h) AND is_unresolved = 0",
            {"h": handle},
        ).fetchone()
        if already:
            # Already present — append the source label if missing, but don't count as "added."
            existing = conn.execute(
                "SELECT discovery_sources_json FROM kol_candidates WHERE candidate_id = :cid",
                {"cid": already["candidate_id"]},
            ).fetchone()
            sources = json.loads(existing["discovery_sources_json"] or "[]")
            if source not in sources:
                sources.append(source)
                conn.execute(
                    "UPDATE kol_candidates SET discovery_sources_json = :s "
                    "WHERE candidate_id = :cid",
                    {"s": json.dumps(sources), "cid": already["candidate_id"]},
                )
                conn.commit()
            continue

        upsert_candidate(
            conn,
            handle=handle,
            display_name=ent["display_name"],
            discovery_source=source,
        )
        added += 1
    return added


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def run_crossref() -> CrossrefSummary:
    summary = CrossrefSummary()
    with open_db() as conn:
        # Tier-2 first so Match-pass picks up newly-added rows.
        summary.tier2_added = _tier2_pass(conn)
        matched, written = _match_pass(conn)
        summary.matched = matched
        summary.relationships_written = written
    return summary
