"""SableKOL CLI entry point.

`sable-kol` exposes the bank ETL pipeline (ingest → classify → crossref),
diagnostics (bank stats / dump / resolve), the matcher (`find`), and the
gold-set evaluation harness (`eval`).

See PLAN.md for the full surface and design rationale.
"""
from __future__ import annotations

import click

from sable_kol import __version__


@click.group()
@click.version_option(__version__, prog_name="sable-kol")
def cli() -> None:
    """SableKOL — bank-backed KOL discovery and matching."""


# ---------------------------------------------------------------------------
# ETL pipeline (stages 1, 3, 4)
# ---------------------------------------------------------------------------

@cli.command()
@click.option(
    "--list-export",
    "list_export",
    type=click.Path(exists=True, dir_okay=False, readable=True),
    required=True,
    help="Path to manually-exported X list members file (HTML or JSON).",
)
@click.option(
    "--source-id",
    default="cahit_list",
    show_default=True,
    help="Discovery-source label written to discovery_sources_json.",
)
def ingest(list_export: str, source_id: str) -> None:
    """ETL Stage 1 — parse an exported X list and upsert candidates."""
    from sable_kol.ingest import run_ingest
    summary = run_ingest(list_export, source_id=source_id)
    click.echo(
        f"ingest: {summary.parsed} parsed, {summary.inserted} new, "
        f"{summary.updated} updated, {summary.conflicts} conflicts"
    )


@cli.command()
@click.option(
    "--limit",
    type=int,
    default=None,
    help="Cap rows classified per run (default: classify all unclassified).",
)
@click.option(
    "--force",
    is_flag=True,
    help="Re-classify rows that already have archetype/sector tags.",
)
def classify(limit: int | None, force: bool) -> None:
    """ETL Stage 3 — Haiku archetype + sector classifier."""
    from sable_kol.classify import run_classify
    summary = run_classify(limit=limit, force=force)
    click.echo(
        f"classify: {summary.classified} rows, "
        f"{summary.dropped} dropped, ${summary.cost_usd:.2f} spent"
    )


@cli.command()
def crossref() -> None:
    """ETL Stage 4 — join bank against sable.db entities (Tier 2 + sable_relationship)."""
    from sable_kol.crossref import run_crossref
    summary = run_crossref()
    click.echo(
        f"crossref: {summary.matched} matched, {summary.tier2_added} Tier-2 added"
    )


@cli.command()
@click.option(
    "--score-only",
    is_flag=True,
    help="Recompute kol_strength_score from existing fields. No paid calls.",
)
@click.option(
    "--refresh",
    is_flag=True,
    help="Force-refetch SocialData even within the 7-day TTL.",
)
@click.option(
    "--limit",
    type=int,
    default=None,
    help="Cap number of rows enriched per run.",
)
@click.option(
    "--grok-import",
    "grok_import",
    type=click.Path(exists=True, dir_okay=False, readable=True),
    default=None,
    help="Import a Grok JSON response (free, no SocialData calls). Tolerant of truncated arrays.",
)
@click.option(
    "--cross-platform-import",
    "cross_platform_import",
    type=click.Path(exists=True, dir_okay=False, readable=True),
    default=None,
    help="Import a cross-platform JSON (Instagram/TikTok/Threads/etc handle + follower data).",
)
def enrich(score_only: bool, refresh: bool, limit: int | None, grok_import: str | None, cross_platform_import: str | None) -> None:
    """ETL Stage 5 — SocialData enrichment + kol_strength_score computation."""
    if grok_import:
        from sable_kol.grok_import import run_grok_import
        s = run_grok_import(grok_import)
        click.echo(
            f"enrich --grok-import: parsed {s.parsed}, updated {s.updated}, "
            f"not_found {s.not_found}, rescored {s.rescored}"
        )
        return
    if cross_platform_import:
        from sable_kol.cross_platform import run_cross_platform_import
        s = run_cross_platform_import(cross_platform_import)
        click.echo(
            f"enrich --cross-platform-import: parsed {s.parsed}, updated {s.updated}, "
            f"not_found {s.not_found}, platforms {s.platforms_updated}"
        )
        return
    if score_only:
        from sable_kol.enrich import run_score_only
        s = run_score_only()
        click.echo(f"enrich --score-only: rescored {s.rescored} rows")
        return
    from sable_kol.enrich import run_enrich
    s = run_enrich(refresh=refresh, limit=limit)
    click.echo(
        f"enrich: {s.enriched} enriched, {s.skipped_fresh} fresh (skipped), "
        f"{s.errors} errors, ${s.cost_usd:.2f} spent, {s.rescored} rescored"
    )


# ---------------------------------------------------------------------------
# Bank diagnostics
# ---------------------------------------------------------------------------

@cli.group()
def bank() -> None:
    """Bank diagnostics and conflict resolution."""


@bank.command(name="stats")
def bank_stats() -> None:
    """Print row counts by status, source mix, classifier coverage, open conflicts."""
    from sable_kol.diagnostics import print_bank_stats
    print_bank_stats()


@bank.command(name="dump")
@click.option("--handle", required=True, help="Normalized handle to inspect.")
def bank_dump(handle: str) -> None:
    """Pretty-print a single candidate row (decoded JSON)."""
    from sable_kol.diagnostics import print_bank_row
    print_bank_row(handle)


@bank.command(name="resolve")
@click.option("--conflict", "conflict_id", required=True, type=int)
@click.option(
    "--action",
    type=click.Choice(["merge", "supersede", "discard"]),
    required=True,
)
def bank_resolve(conflict_id: int, action: str) -> None:
    """Manually resolve a handle resolution conflict."""
    from sable_kol.diagnostics import resolve_conflict
    resolve_conflict(conflict_id, action)


# ---------------------------------------------------------------------------
# Matcher
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--org", "org_id", help="Sable client/prospect org_id (path i).")
@click.option("--handle", "external_handle", help="External Twitter handle (path ii).")
@click.option("--sector", "sector", help="Sector tag for path (ii). Required with --handle.")
@click.option(
    "--themes",
    default=None,
    help="Comma-separated theme keywords for path (ii) (e.g. 'yield,rwa,solana').",
)
@click.option(
    "--paid-enrich",
    is_flag=True,
    help="Path (ii) only — opt in to one cached SocialData profile call (~$0.002, TTL 7d).",
)
@click.option(
    "--refresh-paid",
    is_flag=True,
    help="Force-refresh path-(ii) paid profile cache regardless of TTL.",
)
@click.option("--limit", type=int, default=20, show_default=True)
@click.option(
    "--output",
    "output_format",
    type=click.Choice(["table", "json"]),
    default="table",
    show_default=True,
)
def find(
    org_id: str | None,
    external_handle: str | None,
    sector: str | None,
    themes: str | None,
    paid_enrich: bool,
    refresh_paid: bool,
    limit: int,
    output_format: str,
) -> None:
    """Rank KOL candidates for a project.

    Path (i):  sable-kol find --org <org_id>
    Path (ii): sable-kol find --handle <h> --sector <s> [--themes ...] [--paid-enrich]
    """
    from sable_kol.match import run_find
    if not org_id and not external_handle:
        raise click.UsageError("Specify either --org or --handle.")
    if external_handle and not sector:
        raise click.UsageError("--handle requires --sector.")
    if org_id and external_handle:
        raise click.UsageError("--org and --handle are mutually exclusive.")
    run_find(
        org_id=org_id,
        external_handle=external_handle,
        sector=sector,
        themes=[t.strip() for t in themes.split(",")] if themes else [],
        paid_enrich=paid_enrich,
        refresh_paid=refresh_paid,
        limit=limit,
        output_format=output_format,
    )


# ---------------------------------------------------------------------------
# Eval
# ---------------------------------------------------------------------------

@cli.command(name="eval")
@click.option(
    "--gold-set",
    "gold_set_path",
    type=click.Path(exists=True, dir_okay=False),
    default="eval/gold_set.yaml",
    show_default=True,
)
def eval_cmd(gold_set_path: str) -> None:
    """Run gold-set bank-coverage and ranker-recall reports."""
    from sable_kol.eval import run_eval
    run_eval(gold_set_path)


# ---------------------------------------------------------------------------
# Bulk fetch — followers / following extraction (SolStitch follow-graph plan)
# ---------------------------------------------------------------------------

@cli.group(name="bulk-fetch")
def bulk_fetch() -> None:
    """Cursor-paginated SocialData extraction for follow-graph analysis.

    Both subcommands use the **"Limited Access"** SocialData endpoints
    (/twitter/followers/list, /twitter/friends/list). Verify access with the
    Phase 0.5 contract spike before running at scale.
    """


@bulk_fetch.command(name="followers")
@click.argument("handle")
@click.option(
    "--client",
    "client_id",
    default="_external",
    show_default=True,
    help="Client this run belongs to (writes kol_extract_runs.client_id, "
         "migration 039). Pass the actual client when running production "
         "extracts ('solstitch', 'tig', etc.) so per-client graph queries "
         "stay clean. Default '_external' is the catch-all sentinel.",
)
@click.option(
    "--floor-followers",
    type=int,
    default=500,
    show_default=True,
    help="Drop returned profiles with followers_count below this floor.",
)
@click.option(
    "--page-limit",
    type=int,
    default=None,
    help="Stop after N pages (for testing / cost-capped runs).",
)
@click.option(
    "--output",
    "output_path",
    type=click.Path(dir_okay=False, writable=True),
    default=None,
    help="If set, write yielded profile dicts as JSONL to this path.",
)
def bulk_fetch_followers(
    handle: str,
    client_id: str,
    floor_followers: int,
    page_limit: int | None,
    output_path: str | None,
) -> None:
    """Pull followers for HANDLE via /twitter/followers/list (paginated)."""
    import json as _json

    from sable_kol.db import open_db, normalize_handle
    from sable_kol import socialdata_bulk as bulk

    h = normalize_handle(handle)
    with open_db() as conn:
        uid = bulk.resolve_user_id(conn, h)
        if uid is None:
            raise click.ClickException(f"could not resolve user_id for @{h}")
        run = bulk.create_run(
            conn,
            target_handle=h,
            target_user_id=uid,
            extract_type="followers",
            client_id=client_id,
        )
        click.echo(f"started run {run.run_id} for @{h} (user_id={uid})")
        n = 0
        edge_batch: list[dict] = []
        out_fh = open(output_path, "w", encoding="utf-8") if output_path else None
        try:
            for profile in bulk.pull_followers(
                conn,
                run=run,
                floor_followers=floor_followers,
                page_limit=page_limit,
            ):
                n += 1
                if out_fh is not None:
                    out_fh.write(_json.dumps(profile) + "\n")
                # In a 'followers' extract, each yielded profile is a follower
                # of the target. Edge orientation: follower → target.
                edge_batch.append({
                    "follower_id": profile.get("id_str") or str(profile["id"]),
                    "follower_handle": profile.get("screen_name"),
                    "followed_id": uid,
                    "followed_handle": h,
                })
                if len(edge_batch) >= 100:
                    bulk.insert_edges(conn, run_id=run.run_id, edges=edge_batch)
                    edge_batch.clear()
        finally:
            if edge_batch:
                bulk.insert_edges(conn, run_id=run.run_id, edges=edge_batch)
            if out_fh is not None:
                out_fh.close()
        final = bulk.get_run(conn, run.run_id)
        completed = bool(final and final.cursor_completed)
        click.echo(
            f"run {run.run_id}: {n} profiles kept, "
            f"{(final.pages_fetched if final else 0)} pages, "
            f"${(final.cost_usd_logged if final else 0):.4f} logged, "
            f"{'completed' if completed else 'partial'}"
        )


@bulk_fetch.command(name="following")
@click.argument("handle")
@click.option(
    "--client",
    "client_id",
    default="_external",
    show_default=True,
    help="Client this run belongs to (writes kol_extract_runs.client_id, "
         "migration 039). See `bulk-fetch followers --help` for details.",
)
@click.option(
    "--max-following",
    type=int,
    default=1000,
    show_default=True,
    help="Skip the target if their friends_count exceeds this cap.",
)
@click.option(
    "--page-limit",
    type=int,
    default=None,
    help="Stop after N pages.",
)
@click.option(
    "--output",
    "output_path",
    type=click.Path(dir_okay=False, writable=True),
    default=None,
)
def bulk_fetch_following(
    handle: str,
    client_id: str,
    max_following: int,
    page_limit: int | None,
    output_path: str | None,
) -> None:
    """Pull a target's friends list via /twitter/friends/list (paginated)."""
    import json as _json

    from sable_kol.db import open_db, normalize_handle
    from sable_kol import socialdata_bulk as bulk

    h = normalize_handle(handle)
    with open_db() as conn:
        uid = bulk.resolve_user_id(conn, h)
        if uid is None:
            raise click.ClickException(f"could not resolve user_id for @{h}")
        run = bulk.create_run(
            conn,
            target_handle=h,
            target_user_id=uid,
            extract_type="following",
            client_id=client_id,
        )
        click.echo(f"started run {run.run_id} for @{h} (user_id={uid})")
        n = 0
        edge_batch: list[dict] = []
        out_fh = open(output_path, "w", encoding="utf-8") if output_path else None
        try:
            for profile in bulk.pull_following(
                conn,
                run=run,
                max_following=max_following,
                page_limit=page_limit,
            ):
                n += 1
                if out_fh is not None:
                    out_fh.write(_json.dumps(profile) + "\n")
                # In a 'following' extract, each yielded profile is followed
                # by the target. Edge orientation: target → followed.
                edge_batch.append({
                    "follower_id": uid,
                    "follower_handle": h,
                    "followed_id": profile.get("id_str") or str(profile["id"]),
                    "followed_handle": profile.get("screen_name"),
                })
                if len(edge_batch) >= 100:
                    bulk.insert_edges(conn, run_id=run.run_id, edges=edge_batch)
                    edge_batch.clear()
        finally:
            if edge_batch:
                bulk.insert_edges(conn, run_id=run.run_id, edges=edge_batch)
            if out_fh is not None:
                out_fh.close()
        final = bulk.get_run(conn, run.run_id)
        completed = bool(final and final.cursor_completed)
        click.echo(
            f"run {run.run_id}: {n} profiles, "
            f"{(final.pages_fetched if final else 0)} pages, "
            f"${(final.cost_usd_logged if final else 0):.4f} logged, "
            f"{'completed' if completed else 'partial'}"
        )


# ---------------------------------------------------------------------------
# Follow-graph analysis
# ---------------------------------------------------------------------------

@cli.group(name="follow-graph")
def follow_graph_grp() -> None:
    """Co-follow / kingmaker / cluster analysis over kol_follow_edges."""


@follow_graph_grp.command(name="analyze")
@click.option(
    "--kol-set",
    "kol_set_path",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help="JSON file with an array of normalized handles to restrict the analysis.",
)
@click.option(
    "--extract-type",
    type=click.Choice(["following", "followers"]),
    default="following",
    show_default=True,
)
@click.option(
    "--min-kingmaker",
    type=int,
    default=30,
    show_default=True,
    help="Min co-follow count for an account to be tagged a kingmaker.",
)
@click.option(
    "--thresholds",
    default="0.05,0.15,0.30",
    show_default=True,
    help="Comma-separated Jaccard thresholds for clustering.",
)
@click.option(
    "--output",
    "output_path",
    type=click.Path(dir_okay=False, writable=True),
    default=None,
    help="Write the analysis as JSON to this path.",
)
def follow_graph_analyze(
    kol_set_path: str | None,
    extract_type: str,
    min_kingmaker: int,
    thresholds: str,
    output_path: str | None,
) -> None:
    """Build co-follow matrix, find kingmakers, cluster KOLs (multi-threshold)."""
    import json as _json

    from sable_kol.db import open_db
    from sable_kol import follow_graph as fg

    handles: list[str] | None = None
    if kol_set_path:
        with open(kol_set_path, "r", encoding="utf-8") as fh:
            data = _json.load(fh)
            if isinstance(data, list):
                handles = [str(x).lstrip("@").lower().strip() for x in data]
            elif isinstance(data, dict) and isinstance(data.get("handles"), list):
                handles = [
                    str(x).lstrip("@").lower().strip() for x in data["handles"]
                ]
            else:
                raise click.ClickException("kol-set file must be JSON array or {handles: [...]}")

    th = tuple(float(x) for x in thresholds.split(",") if x.strip())

    with open_db() as conn:
        m = fg.build_co_follow_matrix(
            conn, kol_handles=handles, extract_type=extract_type
        )
        kingmakers = fg.identify_kingmakers(m, min_count=min_kingmaker)
        clusters_at = fg.cluster_kols(m, thresholds=th)
        labeled: dict[str, list[dict]] = {}
        for t, clusters in clusters_at.items():
            labeled[str(t)] = []
            for c in clusters:
                c.label = fg.cluster_label_via_tfidf(c.members, m)
                labeled[str(t)].append(
                    {
                        "cluster_id": c.cluster_id,
                        "label": c.label,
                        "size": len(c.members),
                        "members": c.members,
                    }
                )

        payload = {
            "summary": {
                "rows": len(m.rows),
                "cols": len(m.cols),
                "edges_loaded": m.nnz if m.rows and m.cols else 0,
                "extract_type": extract_type,
            },
            "kingmakers": [
                {"handle": k.handle, "follower_count_in_pool": k.follower_count_in_pool}
                for k in kingmakers
            ],
            "clusters_at_threshold": labeled,
        }
        if output_path:
            with open(output_path, "w", encoding="utf-8") as fh:
                _json.dump(payload, fh, indent=2)
            click.echo(f"wrote {output_path}")
        else:
            click.echo(_json.dumps(payload, indent=2))


# ---------------------------------------------------------------------------
# Outreach plan
# ---------------------------------------------------------------------------

@cli.command(name="outreach-plan")
@click.option("--top-k", type=int, default=200, show_default=True)
@click.option(
    "--tier-a", "tier_a", type=int, default=100_000, show_default=True,
    help="Reach floor for Tier-A (best-of cross-platform max).",
)
@click.option("--tier-b", "tier_b", type=int, default=10_000, show_default=True)
@click.option("--tier-c", "tier_c", type=int, default=1_000, show_default=True)
@click.option(
    "--manual-pins",
    "manual_pins",
    default=None,
    help="Comma-separated handles to force into Tier-A.",
)
@click.option(
    "--output",
    "output_path",
    type=click.Path(dir_okay=False, writable=True),
    default=None,
    help="Output JSON path (Tier breakdown + per-target rows).",
)
def outreach_plan_cmd(
    top_k: int,
    tier_a: int,
    tier_b: int,
    tier_c: int,
    manual_pins: str | None,
    output_path: str | None,
) -> None:
    """Build a tiered SolStitch-style outreach plan from the bank."""
    import json as _json

    from sable_kol.db import open_db
    from sable_kol.outreach_plan import build_plan, to_json_payload

    pins = (
        {h.strip().lstrip("@").lower() for h in manual_pins.split(",") if h.strip()}
        if manual_pins
        else None
    )
    with open_db() as conn:
        targets = build_plan(
            conn,
            top_k=top_k,
            tier_a_threshold=tier_a,
            tier_b_threshold=tier_b,
            tier_c_threshold=tier_c,
            manual_pins=pins,
        )
        payload = to_json_payload(targets)
        if output_path:
            with open(output_path, "w", encoding="utf-8") as fh:
                _json.dump(payload, fh, indent=2)
            click.echo(f"wrote {output_path}")
        else:
            click.echo(_json.dumps(payload, indent=2))


# ---------------------------------------------------------------------------
# Regenerate (Hetzner systemd timer entry point)
# ---------------------------------------------------------------------------

@cli.command(name="regenerate")
@click.argument("client_id")
@click.option("--skip-classify", is_flag=True,
              help="Skip Haiku classification of unclassified candidates.")
@click.option("--skip-score", is_flag=True,
              help="Skip kol_strength_score recompute.")
@click.option("--output-dir",
              default=None,
              type=click.Path(file_okay=False),
              help="Override outreach output dir (default: per-client).")
@click.option("--network-max-nodes", type=int, default=5000, show_default=True)
@click.option("--network-suffix", default="interactive", show_default=True)
@click.option("--json", "as_json", is_flag=True,
              help="Print summary as JSON (for systemd journal / log scrape).")
def regenerate_cmd(
    client_id: str,
    skip_classify: bool,
    skip_score: bool,
    output_dir: str | None,
    network_max_nodes: int,
    network_suffix: str,
    as_json: bool,
) -> None:
    """Refresh all deliverables for one client (no SocialData spend).

    Steps:
      1. Classify any unclassified candidates (Haiku — cheap)
      2. Recompute kol_strength_score
      3. Build outreach plan (filtered + _full variants + symlinks)
      4. Build network graph (GEXF + interactive HTML)

    The cron / systemd path uses --json for structured logging. Operators
    can also run ad-hoc.
    """
    import json as _json
    from dataclasses import asdict
    from pathlib import Path as _Path
    from sable_kol.regenerate import run_regenerate

    summary = run_regenerate(
        client_id,
        skip_classify=skip_classify,
        skip_score=skip_score,
        output_dir=_Path(output_dir) if output_dir else None,
        network_max_nodes=network_max_nodes,
        network_suffix=network_suffix,
    )

    if as_json:
        click.echo(_json.dumps(asdict(summary), indent=2))
    else:
        click.echo(f"client       {summary.client_id}")
        click.echo(f"duration     {summary.duration_seconds}s")
        click.echo(
            f"classify     classified={summary.classify_classified} "
            f"dropped={summary.classify_dropped} "
            f"cost=${summary.classify_cost_usd:.2f}"
        )
        click.echo(f"score        rescored={summary.score_rescored}")
        click.echo(f"outreach     {len(summary.outreach_files_written)} files")
        for f in summary.outreach_files_written[:8]:
            click.echo(f"  - {f}")
        click.echo(f"network      {len(summary.network_files_written)} files")
        for f in summary.network_files_written[:4]:
            click.echo(f"  - {f}")
        if summary.errors:
            click.echo(f"errors       {len(summary.errors)}")
            for e in summary.errors:
                click.echo(f"  ! {e}")
            raise click.exceptions.Exit(1)


if __name__ == "__main__":
    cli()
