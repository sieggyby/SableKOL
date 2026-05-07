"""Bank diagnostics CLI helpers — stats, dump, conflict resolution."""
from __future__ import annotations

import json
import sys

import click

from sable_kol.db import (
    bank_stats,
    get_candidate_by_handle,
    list_open_conflicts,
    open_db,
    update_conflict_state,
)


def print_bank_stats() -> None:
    with open_db() as conn:
        stats = bank_stats(conn)
        sources = _source_distribution(conn)
    click.echo(f"Total live candidates : {stats['total_live']}")
    click.echo(f"  classified          : {stats['classified']}")
    click.echo(f"  unclassified        : {stats['unclassified']}")
    click.echo(f"Unresolved rows       : {stats['unresolved_rows']}")
    click.echo(f"Open conflicts        : {stats['open_conflicts']}")
    click.echo("")
    click.echo("By status:")
    for status, n in sorted(stats["by_status"].items(), key=lambda kv: -kv[1]):
        click.echo(f"  {status:<12} {n}")
    click.echo("")
    click.echo("By discovery source:")
    for source, n in sorted(sources.items(), key=lambda kv: -kv[1])[:15]:
        click.echo(f"  {source:<24} {n}")


def _source_distribution(conn) -> dict[str, int]:
    """Count how often each source label appears across discovery_sources_json."""
    rows = conn.execute(
        "SELECT discovery_sources_json FROM kol_candidates WHERE is_unresolved = 0"
    ).fetchall()
    counter: dict[str, int] = {}
    for r in rows:
        for s in json.loads(r["discovery_sources_json"] or "[]"):
            counter[s] = counter.get(s, 0) + 1
    return counter


def print_bank_row(handle: str) -> None:
    with open_db() as conn:
        cand = get_candidate_by_handle(conn, handle)
    if cand is None:
        click.echo(f"No live candidate for handle: {handle}", err=True)
        sys.exit(1)
    click.echo(json.dumps(_candidate_to_dict(cand), indent=2, default=str))


def _candidate_to_dict(c) -> dict:
    return {
        "candidate_id": c.candidate_id,
        "twitter_id": c.twitter_id,
        "handle_normalized": c.handle_normalized,
        "is_unresolved": c.is_unresolved,
        "handle_history": c.handle_history,
        "display_name": c.display_name,
        "bio_snapshot": c.bio_snapshot,
        "followers_snapshot": c.followers_snapshot,
        "discovery_sources": c.discovery_sources,
        "first_seen_at": c.first_seen_at,
        "last_seen_at": c.last_seen_at,
        "archetype_tags": c.archetype_tags,
        "sector_tags": c.sector_tags,
        "sable_relationship": c.sable_relationship,
        "enrichment_tier": c.enrichment_tier,
        "last_enriched_at": c.last_enriched_at,
        "status": c.status,
        "manual_notes": c.manual_notes,
    }


def resolve_conflict(conflict_id: int, action: str) -> None:
    """Manually resolve a kol_handle_resolution_conflicts row.

    Actions:
      * ``merge``      — keep the live row, fold incoming data into history,
                         drop the unresolved row.
      * ``supersede``  — promote the unresolved row to live (mark live row's
                         is_unresolved=1 instead).
      * ``discard``    — drop the unresolved row entirely.
    """
    with open_db() as conn:
        row = conn.execute(
            "SELECT * FROM kol_handle_resolution_conflicts WHERE conflict_id = :id",
            {"id": conflict_id},
        ).fetchone()
        if row is None:
            click.echo(f"No conflict #{conflict_id}", err=True)
            sys.exit(1)
        if row["resolution_state"] != "open":
            click.echo(
                f"Conflict #{conflict_id} already resolved "
                f"(state={row['resolution_state']})",
                err=True,
            )
            sys.exit(1)

        incoming = row["incoming_candidate_id"]
        existing = row["existing_candidate_id"]

        if action == "discard":
            # Soft-drop: status='dropped' + is_unresolved=1. We can't DELETE
            # because kol_handle_resolution_conflicts.incoming_candidate_id
            # FKs to this row, and a hard delete would violate the FK or
            # require schema-level cascade. Soft-drop preserves the audit trail.
            conn.execute(
                "UPDATE kol_candidates SET status = 'dropped', is_unresolved = 1 "
                "WHERE candidate_id = :cid",
                {"cid": incoming},
            )
            conn.commit()
            update_conflict_state(
                conn,
                conflict_id=conflict_id,
                state="discarded",
                notes="discarded by operator (soft-dropped)",
            )
            click.echo(f"Discarded incoming row #{incoming} (soft-drop).")
            return

        if action == "supersede":
            # Demote the existing live row.
            conn.execute(
                "UPDATE kol_candidates SET is_unresolved = 1 WHERE candidate_id = :cid",
                {"cid": existing},
            )
            # Promote the incoming row.
            conn.execute(
                "UPDATE kol_candidates SET is_unresolved = 0 WHERE candidate_id = :cid",
                {"cid": incoming},
            )
            conn.commit()
            update_conflict_state(
                conn,
                conflict_id=conflict_id,
                state="superseded",
                notes="incoming promoted; existing demoted to unresolved",
            )
            click.echo(f"Superseded #{existing} with #{incoming}.")
            return

        if action == "merge":
            # Append incoming handle to existing's handle_history; drop incoming.
            ex = conn.execute(
                "SELECT handle_history_json FROM kol_candidates WHERE candidate_id = :cid",
                {"cid": existing},
            ).fetchone()
            history = json.loads(ex["handle_history_json"] or "[]")
            in_row = conn.execute(
                "SELECT handle_normalized, twitter_id FROM kol_candidates WHERE candidate_id = :cid",
                {"cid": incoming},
            ).fetchone()
            history.append({
                "handle": in_row["handle_normalized"],
                "twitter_id": in_row["twitter_id"],
                "merged_via_conflict": conflict_id,
            })
            conn.execute(
                "UPDATE kol_candidates SET handle_history_json = :h WHERE candidate_id = :cid",
                {"h": json.dumps(history), "cid": existing},
            )
            # Soft-drop the incoming row (FK from conflicts row prevents hard DELETE).
            conn.execute(
                "UPDATE kol_candidates SET status = 'dropped', is_unresolved = 1 "
                "WHERE candidate_id = :cid",
                {"cid": incoming},
            )
            conn.commit()
            update_conflict_state(
                conn,
                conflict_id=conflict_id,
                state="merged",
                notes=f"merged into candidate #{existing}",
            )
            click.echo(f"Merged #{incoming} into #{existing}.")
            return

        raise click.UsageError(f"Unknown action: {action}")
