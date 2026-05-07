"""SolStitch follow-graph network export.

Produces two artifacts from the kol_follow_edges + kol_extract_runs data:

  * solstitch_network_<date>.gexf — Gephi-format XML. Open in Gephi for
    publication-quality layouts (ForceAtlas2 + community detection + size
    encoding). Best polish.
  * solstitch_network_<date>.html — self-contained vis.js page, loads from
    CDN. Drag-and-drop the file into a browser for instant inspection.

Node selection (default ~1K target — controlled by --max-nodes):
  * 200 outreach-plan candidates (always retained)
  * 66 surveyed cohort KOLs (always retained)
  * Top kingmakers ranked by `in_pool + sector_bonus`, where sector_bonus is
    +5 for accounts whose bank sector_tags intersect the SolStitch-thesis
    set {fashion, culture, art, design, nfts, streetwear, music, social,
    creator, media}. This bumps on-thesis kingmakers above generic
    crypto-royalty (e.g. @cz_binance, @vitalikbuterin) when budget is tight.

Pass --max-nodes 5000 (or higher) to revert to the wide view.

Edge selection:
  * Only edges where BOTH endpoints are in the visualized set.

Node attributes:
  * `followers` — followers_snapshot from bank (0 if unknown)
  * `in_pool` — count of cohort KOLs who follow this node
  * `tier` — A/B/C/unranked from outreach plan, or "kingmaker" / "cohort"
  * `sector` — primary sector tag (or empty)
  * `archetype` — primary archetype tag (or empty)
  * `role` — one of: "candidate" (in outreach plan), "cohort" (we pulled their
    followings), "kingmaker" (followed by ≥N cohort), "both"

Visualization recipe (Gephi):
  1. Open the .gexf file
  2. Statistics → run Modularity (community detection)
  3. Appearance → Nodes → Size → followers (logarithmic, range 8–80)
  4. Appearance → Nodes → Color → in_pool (sequential palette)
  5. Layout → ForceAtlas2 with `Prevent Overlap` + `LinLog mode`
  6. Filters → Topology → Giant Component for the connected core
"""
from __future__ import annotations

import argparse
import html
import json
import math
import os
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from sable_kol.db import open_db
from sable_kol.filters import (
    is_organization as _shared_is_organization,
    is_celebrity as _shared_is_celebrity,
)


DOWNLOADS = Path.home() / "Downloads"
DATE_SLUG = datetime.now().strftime("%Y_%m_%d")

PLAN_JSON = DOWNLOADS / f"solstitch_outreach_plan_{DATE_SLUG}_post_tier_a_sweep.json"
PLAN_JSON_FALLBACK = DOWNLOADS / "solstitch_outreach_plan_2026_05_06.json"

MIN_KINGMAKER = 4  # accounts followed by >= this many cohort KOLs are eligible

# Sectors that get a +5 ranking bonus when the node-budget is tight, so
# on-thesis SolStitch-relevant kingmakers bubble above generic crypto-royalty.
THESIS_SECTORS = {
    "fashion", "culture", "art", "design", "nfts",
    "streetwear", "music", "social", "creator", "media",
}
SECTOR_BONUS = 5.0


# Known organization / brand / protocol / platform accounts. The graph
# defaults to PEOPLE only — orgs are useless outreach targets (no one
# reads the DMs); the operator wants to message the humans behind the
# brands, not the brand handles. Toggle "Show organizations" in the
# panel to override per-view.
#
# Maintenance: when the kingmaker list surfaces a new org we should
# remove from the network, append it here and re-run the script.
ORG_DENYLIST = {
    # Marketplaces / platforms
    "opensea", "rarible", "blur_io", "looksrare", "magic_eden", "magiceden",
    "rugradio", "nft_nyc", "coindesk", "highsnobiety_official",
    # Brands / projects (audience targets are orgs by definition)
    "thefabricant", "9dccxyz", "doji_com",
    "rtfkt", "yugalabs", "boredapeyc", "cryptopunks", "decentraland",
    "worldofwomenxyz", "othersidemeta",
    # Protocols / chains / infra
    "ethereum", "0xpolygon", "buildonbase", "ledger", "metamask",
    "infura_io", "alchemy", "uniswap", "binance", "coinbase",
    "ipfsofficial", "aave", "compoundfinance", "makerdao", "starknet",
    # Cohort-extracted brand accounts (surfaced in kingmaker output)
    "artblocks_io", "showstudio", "monaverse", "dressxcom", "pixelvault_",
    "infiniteobjects", "krwn_studio", "mntge_io", "auroboros_ltd",
    "mutani_io", "dapewives", "axiesisters", "abstractorsnft",
    "themetajuice", "another1_io", "hellometaversal", "arianeeproject",
    "aeoniumsky", "slickcitynft", "thedigitaldogs", "sailormarsnft",
    "fashionweekonline", "iyk_app", "moodyowlnft", "artontezos_",
    "trilitech", "spatial_io", "rplanetnft", "unionavatars",
    "flowergirlsnft", "toygersofficial", "cheb_inc", "remx_xyz",
    "cuedotfun", "vcaresidency", "verticalcrypto", "prooofofpeople",
    "betterasaweb", "nftinsider_io", "abdroid_xyz", "aventurinelabs",
    "tributelabsxyz", "adinonline", "flamingobluexyz", "net__society",
    "nftmorning", "nftfactoryparis", "bemyappofficial", "bemyapp",
    "lvmh", "obvious_official", "obv_ious",
}


# Suffixes / substrings strongly correlated with org accounts.
# Used as fallback heuristic for handles not in the explicit denylist.
ORG_HANDLE_SUFFIXES = (
    "_io", "_xyz", "_app", "_hq", "_labs", "_studio", "_protocol",
    "_network", "_foundation", "_official", "_dao", "_inc", "_ltd",
    "_eth", "_finance",
)
ORG_HANDLE_SUBSTRINGS = (
    "official", "labs", "studio", "protocol", "foundation",
    "dao", "network", "marketplace", "exchange",
)

# Known PERSON accounts that the heuristic would flag as orgs (override).
# Add as discovered via review of filtered output.
PERSON_ALLOWLIST = {
    "betty_nft",        # real person, Bored Ape co-creator's wife
    "punk6529",         # anon person not org despite handle
    "loomdart",         # operator-pinned person
    "toomuchlag",       # operator-pinned person
}


def is_organization(handle, archetypes, bio):
    """Delegates to sable_kol.filters.is_organization (single source of truth)."""
    return _shared_is_organization(handle, archetypes, bio)


def is_celebrity_node(handle, followers, friends_count):
    return _shared_is_celebrity(handle, followers, friends_count)


def load_outreach_plan() -> dict:
    path = PLAN_JSON if PLAN_JSON.exists() else PLAN_JSON_FALLBACK
    with open(path) as fh:
        return json.load(fh)


def main() -> None:
    from sable_kol.client_config import load_client_config, outreach_output_dir
    from sable_kol.network_axes import CandidateLite, axis_scores

    ap = argparse.ArgumentParser()
    ap.add_argument("--client", required=True,
                    help="Client id (matches a YAML at ~/.sable/clients/<id>.yaml).")
    ap.add_argument("--max-nodes", type=int, default=5000,
                    help="Hard cap on embedded node count (slider trims further client-side). "
                         "Default 5000 lets the slider go up to 'Most'.")
    ap.add_argument("--suffix", default="interactive",
                    help="Filename suffix for the artifacts (default: interactive).")
    ap.add_argument("--output-dir", default=None,
                    help="Override output dir (default: client outreach dir from config).")
    ap.add_argument("--plan-json", default=None,
                    help="Override outreach plan JSON path used to identify the "
                         "candidate set. Default: latest_<mode>_report.json in the "
                         "outreach output dir.")
    args = ap.parse_args()

    config = load_client_config(args.client)
    out_dir = Path(args.output_dir) if args.output_dir else outreach_output_dir(args.client)
    out_dir.mkdir(parents=True, exist_ok=True)
    gexf_path = out_dir / f"{config.client_id}_network_{DATE_SLUG}_{args.suffix}.gexf"
    html_path = out_dir / f"{config.client_id}_network_{DATE_SLUG}_{args.suffix}.html"
    json_path = out_dir / f"{config.client_id}_network_{DATE_SLUG}_{args.suffix}.json"

    plan_path = (
        Path(args.plan_json) if args.plan_json
        else out_dir / f"latest_{config.mode}_report.json"
    )
    if not plan_path.exists():
        # Fall back to whichever dated report.json is most recent.
        candidates = sorted(
            out_dir.glob(f"{config.client_id}_report_*_{config.mode}.json"),
            reverse=True,
        )
        if candidates:
            plan_path = candidates[0]
        else:
            raise FileNotFoundError(
                f"no outreach plan JSON found at {plan_path}; "
                f"run scripts/build_outreach_plan.py --client {args.client} first"
            )
    print(f"client: {config.client_id} ({config.display_name}) · plan: {plan_path.name}")

    with open(plan_path) as fh:
        plan = json.load(fh)
    candidate_handles = {
        t["handle"].lower(): t for tier in ("A", "B", "C", "unranked")
        for t in plan.get("targets", {}).values()
        if False  # placeholder (replaced below)
    }
    # The above structure varies by tier — flatten across all tiers
    candidate_handles = {}
    for tier in ("A", "B", "C", "unranked"):
        for t in plan.get("targets", {}).get(tier, []) or []:
            candidate_handles[t["handle"].lower()] = t
    print(f"outreach-plan candidates: {len(candidate_handles)}")

    with open_db() as conn:
        # 1. Cohort = handles we surveyed (completed following extracts FOR THIS CLIENT).
        # Migration 039: client_id column scopes the cohort. Falls back to '_external'
        # for legacy rows that pre-date the column.
        cohort_rows = conn.execute(
            "SELECT DISTINCT target_handle_normalized FROM kol_extract_runs "
            "WHERE extract_type='following' AND cursor_completed=1 "
            "  AND client_id = :cid",
            {"cid": config.client_id},
        ).fetchall()
        cohort = {r["target_handle_normalized"] for r in cohort_rows}
        print(f"cohort (client={config.client_id}, completed following runs): {len(cohort)}")

        # 2. Build the kingmaker counts (in_pool by followed_handle), scoped to client.
        kingmaker_rows = conn.execute(
            "SELECT e.followed_handle, "
            "       COUNT(DISTINCT r.target_handle_normalized) AS in_pool "
            "FROM kol_follow_edges e "
            "JOIN kol_extract_runs r ON r.run_id = e.run_id "
            "WHERE r.cursor_completed=1 AND r.extract_type='following' "
            "  AND r.client_id = :cid "
            "GROUP BY e.followed_handle",
            {"cid": config.client_id},
        ).fetchall()
        in_pool: dict[str, int] = {}
        for r in kingmaker_rows:
            h = (r["followed_handle"] or "").lower()
            if h:
                in_pool[h] = r["in_pool"]
        kingmakers = {h for h, n in in_pool.items() if n >= MIN_KINGMAKER}
        print(f"kingmakers (in_pool >= {MIN_KINGMAKER}): {len(kingmakers)}")

        # 3. Compose the node set: union of candidates, cohort, kingmakers.
        node_set: set[str] = set()
        node_set.update(candidate_handles.keys())
        node_set.update(cohort)
        node_set.update(kingmakers)
        print(f"union node set: {len(node_set)}")

        # 4. Cap at args.max_nodes. Cohort + candidates are always retained.
        #    Remaining slots filled by kingmakers ranked on:
        #      score = in_pool + sector_bonus
        #    Sector bonus requires the kingmaker to be in the bank with at
        #    least one tag in THESIS_SECTORS. Untagged / unknown rows get 0.
        keep = set(cohort) | set(candidate_handles.keys())
        budget = args.max_nodes - len(keep)
        if budget < 0:
            print(f"WARNING: candidates+cohort ({len(keep)}) > max-nodes ({args.max_nodes})")
            node_set = keep
        elif len(node_set) > args.max_nodes:
            # Pre-pull bank sectors for the kingmaker pool to apply bonus.
            kingmaker_handles = [h for h in kingmakers if h not in keep]
            bank_sectors: dict[str, list[str]] = {}
            chunk = 500
            for i in range(0, len(kingmaker_handles), chunk):
                sub = kingmaker_handles[i:i+chunk]
                placeholders = ",".join("?" * len(sub))
                for r in conn.execute(
                    f"SELECT handle_normalized, sector_tags_json FROM kol_candidates "
                    f"WHERE is_unresolved=0 AND handle_normalized IN ({placeholders})",
                    sub,
                ).fetchall():
                    try:
                        bank_sectors[r["handle_normalized"]] = json.loads(r["sector_tags_json"] or "[]")
                    except Exception:
                        bank_sectors[r["handle_normalized"]] = []

            def score(h: str) -> float:
                base = in_pool.get(h, 0)
                sectors = bank_sectors.get(h, [])
                bonus = SECTOR_BONUS if any(s in THESIS_SECTORS for s in sectors) else 0.0
                return base + bonus

            ranked = sorted(kingmaker_handles, key=lambda h: -score(h))
            keep.update(ranked[:budget])
            node_set = keep
            print(f"trimmed to {args.max_nodes} (sector-bonus +{SECTOR_BONUS} applied): {len(node_set)}")

        # 5. Pull bank info for all nodes (followers, sectors, archetypes).
        bank: dict[str, dict] = {}
        ns = list(node_set)
        chunk = 500
        for i in range(0, len(ns), chunk):
            sub = ns[i:i+chunk]
            placeholders = ",".join("?" * len(sub))
            for r in conn.execute(
                f"SELECT handle_normalized, followers_snapshot, following_count, "
                f"       sector_tags_json, archetype_tags_json, bio_snapshot, display_name "
                f"FROM kol_candidates "
                f"WHERE is_unresolved=0 AND handle_normalized IN ({placeholders})",
                sub,
            ).fetchall():
                bank[r["handle_normalized"]] = dict(r)

        # 6. Pull edges within the node set.
        node_set_lower = node_set
        # SQLite is fastest if we filter at the join level using a temp table,
        # but for a one-shot script we can pull all completed edges and filter in Python.
        # 134K edges is small.
        all_edges = conn.execute(
            "SELECT e.follower_handle, e.followed_handle "
            "FROM kol_follow_edges e "
            "JOIN kol_extract_runs r ON r.run_id = e.run_id "
            "WHERE r.cursor_completed=1 AND r.extract_type='following'"
        ).fetchall()
        edges: list[tuple[str, str]] = []
        for e in all_edges:
            f = (e["follower_handle"] or "").lower()
            t = (e["followed_handle"] or "").lower()
            if not f or not t:
                continue
            if f in node_set_lower and t in node_set_lower:
                edges.append((f, t))
        print(f"edges within node set: {len(edges)}")

    # ---- Build node records with display attributes ----
    nodes: list[dict] = []
    for h in sorted(node_set):
        info = bank.get(h, {})
        followers = info.get("followers_snapshot") or 0
        try:
            sectors = json.loads(info.get("sector_tags_json") or "[]")
        except Exception:
            sectors = []
        try:
            archetypes = json.loads(info.get("archetype_tags_json") or "[]")
        except Exception:
            archetypes = []
        pool = in_pool.get(h, 0)
        is_cand = h in candidate_handles
        is_cohort = h in cohort
        is_kingmaker = h in kingmakers
        # Composite role
        if is_cand and is_cohort:
            role = "candidate+cohort"
        elif is_cand and is_kingmaker:
            role = "candidate+kingmaker"
        elif is_cand:
            role = "candidate"
        elif is_cohort:
            role = "cohort"
        elif is_kingmaker:
            role = "kingmaker"
        else:
            role = "other"
        # Tier (from outreach plan if present)
        tier = candidate_handles.get(h, {}).get("tier", "")
        primary_sector = sectors[0] if sectors else ""
        primary_archetype = archetypes[0] if archetypes else ""
        bio = info.get("bio_snapshot") or ""
        is_org = is_organization(h, archetypes, bio)
        # following_count is on the bank row (migration 034) — pull the value
        # for the celebrity heuristic (followers / friends ratio).
        friends_count = info.get("following_count")
        is_celeb = is_celebrity_node(h, int(followers) if followers else 0, friends_count)
        # Migration plan Q5 + audit P2-1: generic axis_scores.{x,y} per the
        # client's network_axes config. Replaces SolStitch-specific
        # (fashion_score, crypto_score) field names. The renderer reads
        # these as opaque coordinates; labels come from _meta.network_axes.
        cl = CandidateLite(
            bio=bio,
            display_name=info.get("display_name"),
            sector_tags=sectors,
            archetype_tags=archetypes,
        )
        scores = axis_scores(cl, config.network_axes)
        nodes.append({
            "id": h,
            "label": h,
            "followers": int(followers),
            "in_pool": int(pool),
            "role": role,
            "tier": tier,
            "sector": primary_sector,
            "archetype": primary_archetype,
            "is_org": is_org,
            "is_celeb": is_celeb,
            "axis_scores": scores,    # {"x": float, "y": float}
        })

    write_gexf(nodes, edges, gexf_path)
    print(f"wrote {gexf_path}")
    write_html(nodes, edges, html_path)
    print(f"wrote {html_path}")
    write_json(nodes, edges, json_path, config=config)
    print(f"wrote {json_path}")

    # Update latest_network.json symlink so SableWeb's loadKOLNetwork()
    # can pick the freshest file deterministically.
    latest_json = out_dir / f"latest_network_{args.suffix}.json"
    try:
        if latest_json.is_symlink() or latest_json.exists():
            latest_json.unlink()
        latest_json.symlink_to(os.path.relpath(json_path, latest_json.parent))
        print(f"wrote {latest_json}")
    except OSError as exc:
        print(f"  warning: symlink {latest_json.name} failed ({exc}); skipping")
    print()
    print(f"summary: nodes={len(nodes)} edges={len(edges)}")
    role_counts: dict[str, int] = defaultdict(int)
    for n in nodes:
        role_counts[n["role"]] += 1
    for role, cnt in sorted(role_counts.items(), key=lambda x: -x[1]):
        print(f"  {role}: {cnt}")


def write_json(
    nodes: list[dict],
    edges: list[tuple[str, str]],
    path: Path,
    *,
    config,
) -> None:
    """Write the network as a clean JSON file SableWeb (or any consumer) can
    read directly. No HTML wrapper; just nodes + edges + meta.

    Stable schema: bumping `_meta.schema_version` is the contract.
    """
    payload = {
        "_meta": {
            "schema_version": 1,
            "client_id": config.client_id,
            "client_display_name": config.display_name,
            "mode": config.mode,
            "generated_at_utc": datetime.now().isoformat() + "Z",
            "network_axes": {
                "x": {"label": config.network_axes.x.label},
                "y": {"label": config.network_axes.y.label},
            },
            "node_count": len(nodes),
            "edge_count": len(edges),
        },
        "nodes": nodes,
        "edges": [{"from": s, "to": t} for s, t in edges],
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def write_gexf(nodes: list[dict], edges: list[tuple[str, str]], path: Path) -> None:
    """Write Gephi-compatible GEXF 1.3 with node attributes."""
    lines: list[str] = []
    lines.append('<?xml version="1.0" encoding="UTF-8"?>')
    lines.append('<gexf xmlns="http://gexf.net/1.3" version="1.3">')
    lines.append('  <meta lastmodifieddate="' + datetime.now().date().isoformat() + '">')
    lines.append('    <creator>SableKOL build_network_graph.py</creator>')
    lines.append('    <description>SolStitch follow-graph: candidates + cohort + kingmakers</description>')
    lines.append('  </meta>')
    lines.append('  <graph mode="static" defaultedgetype="directed">')
    lines.append('    <attributes class="node">')
    lines.append('      <attribute id="0" title="followers" type="integer"/>')
    lines.append('      <attribute id="1" title="in_pool" type="integer"/>')
    lines.append('      <attribute id="2" title="role" type="string"/>')
    lines.append('      <attribute id="3" title="tier" type="string"/>')
    lines.append('      <attribute id="4" title="sector" type="string"/>')
    lines.append('      <attribute id="5" title="archetype" type="string"/>')
    lines.append('    </attributes>')
    lines.append('    <nodes>')
    for n in nodes:
        nid = html.escape(n["id"])
        label = html.escape(n["label"])
        lines.append(f'      <node id="{nid}" label="{label}">')
        lines.append('        <attvalues>')
        lines.append(f'          <attvalue for="0" value="{n["followers"]}"/>')
        lines.append(f'          <attvalue for="1" value="{n["in_pool"]}"/>')
        lines.append(f'          <attvalue for="2" value="{html.escape(n["role"])}"/>')
        lines.append(f'          <attvalue for="3" value="{html.escape(n["tier"] or "")}"/>')
        lines.append(f'          <attvalue for="4" value="{html.escape(n["sector"] or "")}"/>')
        lines.append(f'          <attvalue for="5" value="{html.escape(n["archetype"] or "")}"/>')
        lines.append('        </attvalues>')
        lines.append('      </node>')
    lines.append('    </nodes>')
    lines.append('    <edges>')
    for i, (s, t) in enumerate(edges):
        lines.append(f'      <edge id="{i}" source="{html.escape(s)}" target="{html.escape(t)}"/>')
    lines.append('    </edges>')
    lines.append('  </graph>')
    lines.append('</gexf>')
    path.write_text("\n".join(lines), encoding="utf-8")


def write_html(nodes: list[dict], edges: list[tuple[str, str]], path: Path) -> None:
    """Self-contained interactive vis.js page with full control panel.

    Embeds the entire node + edge dataset (sorted by importance score) and
    lets the client filter / restyle / animate without re-running Python.

    Controls:
      * Network-size slider (Least → Most): top-N by score
      * Role toggles (candidates / cohort / kingmakers)
      * Tier filter (A/B/C/unranked)
      * Sector filter (primary sector tag, multi-select)
      * Min in-pool slider, min followers slider
      * Distance slider (physics springLength)
      * Size-scale slider (node size multiplier; bigger = more dramatic)
      * Pulsing toggle (heavily-connected nodes oscillate, intensity ∝ in_pool)
      * Color mode: in_pool / sector / tier
      * Search box (jump-and-highlight by handle)
      * Click highlights neighbors; SHIFT+click opens https://x.com/<handle>
    """
    max_in_pool = max((n["in_pool"] for n in nodes), default=1) or 1

    # ---- Compute score so the client can do top-N trimming ----
    # Score = in_pool + thesis_sector_bonus, with candidates+cohort offset
    # so they stay at the top regardless.
    def score(n: dict) -> float:
        base = float(n["in_pool"])
        if n["sector"] in THESIS_SECTORS:
            base += SECTOR_BONUS
        if "candidate" in n["role"] or "cohort" in n["role"]:
            base += 1_000_000  # always retained
        return base

    sorted_nodes = sorted(nodes, key=score, reverse=True)
    # Embed the score so JS can re-sort if needed (and so the slider thresholds
    # still produce a stable top-N set after future re-sorts).
    for n in sorted_nodes:
        n["_score"] = score(n)

    js_nodes = sorted_nodes  # raw attributes; JS does all visual mapping
    js_edges = [{"from": s, "to": t} for s, t in edges]

    # All sectors seen — for the sector filter UI
    all_sectors = sorted({n["sector"] for n in nodes if n["sector"]})

    nodes_json = json.dumps(js_nodes, ensure_ascii=False)
    edges_json = json.dumps(js_edges, ensure_ascii=False)
    sectors_json = json.dumps(all_sectors)

    candidate_count = sum(1 for n in nodes if "candidate" in n["role"])
    cohort_count = sum(1 for n in nodes if "cohort" in n["role"])
    kingmaker_count = sum(1 for n in nodes if n["in_pool"] >= MIN_KINGMAKER)
    cohort_size = sum(1 for n in nodes if n["role"] in ("cohort", "candidate+cohort"))
    org_count = sum(1 for n in nodes if n.get("is_org"))
    celeb_count = sum(1 for n in nodes if n.get("is_celeb"))

    page = HTML_TEMPLATE
    page = page.replace("__NODES__", nodes_json)
    page = page.replace("__EDGES__", edges_json)
    page = page.replace("__SECTORS__", sectors_json)
    page = page.replace("__MAX_IN_POOL__", str(max_in_pool))
    page = page.replace("__TOTAL_NODES__", str(len(nodes)))
    page = page.replace("__TOTAL_EDGES__", str(len(edges)))
    page = page.replace("__CAND_COUNT__", str(candidate_count))
    page = page.replace("__COHORT_COUNT__", str(cohort_count))
    page = page.replace("__KINGMAKER_COUNT__", str(kingmaker_count))
    page = page.replace("__COHORT_SIZE__", str(cohort_size))
    page = page.replace("__ORG_COUNT__", str(org_count))
    page = page.replace("__CELEB_COUNT__", str(celeb_count))
    page = page.replace("__GENERATED_DATE__", datetime.now().date().isoformat())
    path.write_text(page, encoding="utf-8")


# Single-string HTML template. All JS is inlined; vis.js loads from CDN.
HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>SolStitch follow-graph network</title>
<script src="https://unpkg.com/vis-network/standalone/umd/vis-network.min.js"></script>
<style>
  /* Tufte-light: muted neutrals, restrained chrome, data on top. */
  :root {
    --bg: #fafaf7;
    --panel: #fefef9;
    --rule: #d8d6d0;
    --text: #1a1a18;
    --muted: #707068;
    --accent: #b22222;     /* one warm accent reserved for emphasis */
    --link: #4a6d8c;
    --candidate: #2c5282;
    --cohort: #2d6a4f;
    --kingmaker: #b22222;
  }
  html, body { margin:0; padding:0; height:100%; background:var(--bg); color:var(--text);
               font-family: 'ET Book', Georgia, 'Times New Roman', serif;
               overflow: hidden; -webkit-font-smoothing: antialiased; }
  #app { display: flex; height: 100vh; }
  #network { flex: 1; height: 100vh; background: var(--bg); }
  #panel {
    width: 280px; height: 100vh; overflow-y: auto;
    background: var(--panel); border-right: 1px solid var(--rule);
    padding: 18px 20px 30px 20px; box-sizing: border-box;
    font-size: 13px; line-height: 1.5;
  }
  #panel h1 { font-size: 16px; margin: 0 0 2px 0; color: var(--text); font-weight: 400;
              font-style: italic; letter-spacing: 0; }
  #panel .sub { color: var(--muted); margin-bottom: 18px; font-size: 11.5px; font-style: italic; }
  #panel section { margin-bottom: 18px; }
  #panel section + section { padding-top: 14px; border-top: 1px solid var(--rule); }
  #panel label { display: block; color: var(--text); font-size: 11.5px;
                 letter-spacing: 0; margin-bottom: 6px; font-style: italic; }
  #panel .row { display: flex; align-items: center; gap: 8px; margin-bottom: 4px; font-size: 11.5px; }
  #panel input[type=range] { flex: 1; accent-color: var(--accent); }
  #panel input[type=text] { background: #fff; color: var(--text); border: 1px solid var(--rule);
                            padding: 4px 6px; font-size: 12px; border-radius: 0; width: 100%; box-sizing: border-box;
                            font-family: inherit; }
  #panel .check { display: flex; align-items: center; gap: 6px; margin: 2px 0; cursor: pointer;
                  user-select: none; font-size: 12px; color: var(--text); font-style: normal; }
  #panel .check input { margin: 0; accent-color: var(--accent); }
  #panel .swatch { display:inline-block; width:9px; height:9px; vertical-align:middle;
                   margin-right:4px; border-radius:50%; }
  #panel .value { color: var(--text); font-variant-numeric: tabular-nums; min-width: 42px;
                  text-align: right; font-family: 'SF Mono', Menlo, monospace; font-size: 11px; }
  #panel .stat-row { display: flex; justify-content: space-between; color: var(--muted); font-size: 11px; padding: 1px 0; }
  #panel .stat-row span:last-child { color: var(--text); font-variant-numeric: tabular-nums;
                                     font-family: 'SF Mono', Menlo, monospace; }
  #panel .btn { background: transparent; color: var(--link); border: none;
                padding: 2px 0; cursor: pointer; font-size: 12px; margin-right: 12px;
                text-decoration: underline; font-family: inherit; }
  #panel .btn:hover { color: var(--accent); }
  #panel select { background: #fff; color: var(--text); border: 1px solid var(--rule);
                  padding: 4px; font-size: 12px; border-radius: 0; width: 100%; box-sizing: border-box;
                  font-family: inherit; }
  .size-buckets { display: flex; justify-content: space-between; font-size: 9.5px;
                  color: var(--muted); margin-top: 2px; font-style: italic; }
  .size-buckets span { flex: 1; text-align: center; }
  .size-buckets span.active { color: var(--accent); font-weight: 600; font-style: normal; }
  .footer { color: var(--muted); font-size: 10.5px; line-height: 1.5; margin-top: 18px;
            font-style: italic; padding-top: 12px; border-top: 1px solid var(--rule); }
  .help-toggle { color: var(--link); cursor: pointer; font-size: 11px; text-decoration: underline; }
  #help-overlay { position: fixed; top: 16px; left: 300px; background: var(--panel);
                  padding: 16px 22px; border: 1px solid var(--rule); border-radius: 0;
                  font-size: 12.5px; max-width: 420px; display: none; z-index: 999;
                  box-shadow: 0 2px 12px rgba(0,0,0,0.08); }
  #help-overlay h2 { margin: 0 0 8px 0; font-size: 14px; color: var(--text); font-weight: 400; font-style: italic; }
  #help-overlay code { background: #f0eeea; padding: 1px 4px; font-size: 11px;
                       font-family: 'SF Mono', Menlo, monospace; }
  /* Caption block sits inside the canvas — Tufte: explain on the graphic. */
  #caption {
    position: absolute; top: 16px; right: 18px; max-width: 360px;
    font-size: 11.5px; line-height: 1.5; color: var(--text);
    font-style: italic; pointer-events: none; text-align: right;
  }
  #caption .title { font-style: normal; font-size: 13px; color: var(--text); margin-bottom: 4px; }
  #caption .meta { color: var(--muted); margin-top: 6px; font-size: 10.5px; }
</style>
</head>
<body>
<div id="app">
  <div id="panel">
    <h1>SolStitch follow-graph</h1>
    <div class="sub">interactive viewer · <span class="help-toggle" id="help-toggle">help</span></div>

    <section>
      <label>Presets</label>
      <button class="btn" data-preset="default">Default</button>
      <button class="btn" data-preset="fashion">Fashion kingmakers</button><br>
      <button class="btn" data-preset="hidden">Hidden gems</button>
      <button class="btn" data-preset="tier_a">Tier A only</button>
    </section>

    <section>
      <label>Network size</label>
      <input type="range" min="0" max="5" value="2" step="1" id="size-slider">
      <div class="size-buckets">
        <span data-bucket="0">Least</span>
        <span data-bucket="1">Lesser</span>
        <span data-bucket="2" class="active">Less</span>
        <span data-bucket="3">Some</span>
        <span data-bucket="4">More</span>
        <span data-bucket="5">Most</span>
      </div>
    </section>

    <section>
      <label>Roles</label>
      <label class="check"><input type="checkbox" id="r-cand" checked>
        <span class="swatch" style="background:var(--candidate)"></span>
        Outreach-plan candidates (__CAND_COUNT__)</label>
      <label class="check"><input type="checkbox" id="r-cohort" checked>
        <span class="swatch" style="background:var(--cohort)"></span>
        Surveyed cohort (__COHORT_COUNT__)</label>
      <label class="check"><input type="checkbox" id="r-king" checked>
        <span class="swatch" style="background:var(--kingmaker)"></span>
        Kingmakers (__KINGMAKER_COUNT__)</label>
      <div style="margin-top:8px; padding-top:6px; border-top:1px dashed var(--rule)">
        <label class="check"><input type="checkbox" id="r-orgs">
          Show organizations (__ORG_COUNT__ in dataset)</label>
        <div style="font-size:10.5px; color:var(--muted); margin-top:2px; font-style:italic; padding-left:18px">
          orgs filtered by default — DMs go unread
        </div>
        <label class="check" style="margin-top:4px"><input type="checkbox" id="r-celebs">
          Show celebrities (__CELEB_COUNT__ in dataset)</label>
        <div style="font-size:10.5px; color:var(--muted); margin-top:2px; font-style:italic; padding-left:18px">
          Vitalik / CZ / Elon class — won't shill stealth projects
        </div>
      </div>
    </section>

    <section>
      <label>Tier</label>
      <label class="check"><input type="checkbox" class="t-filter" value="A" checked> Tier A</label>
      <label class="check"><input type="checkbox" class="t-filter" value="B" checked> Tier B</label>
      <label class="check"><input type="checkbox" class="t-filter" value="C" checked> Tier C</label>
      <label class="check"><input type="checkbox" class="t-filter" value="unranked" checked> Unranked</label>
    </section>

    <section>
      <label>Sectors</label>
      <select id="sector-filter" multiple size="6"></select>
      <div style="font-size:10px; color:var(--muted); margin-top:3px; font-style:italic">
        cmd-click multi; empty = all
      </div>
    </section>

    <section>
      <label>Numeric filters</label>
      <div class="row"><span style="min-width:84px">Min in-pool</span>
        <input type="range" min="0" max="__MAX_IN_POOL__" value="0" id="min-pool">
        <span class="value" id="min-pool-val">0</span></div>
      <div class="row"><span style="min-width:84px">Min followers</span>
        <input type="range" min="0" max="7" value="0" step="0.5" id="min-followers">
        <span class="value" id="min-followers-val">0</span></div>
    </section>

    <section>
      <label>Layout</label>
      <div class="row"><span style="min-width:84px">Spread</span>
        <input type="range" min="30" max="1500" value="200" id="spring">
        <span class="value" id="spring-val">200</span></div>
      <div class="row"><span style="min-width:84px">Node size</span>
        <input type="range" min="50" max="400" value="150" id="size-scale">
        <span class="value" id="size-scale-val">1.5×</span></div>
      <div class="row" style="margin-top:6px">
        <label class="check" style="margin:0"><input type="checkbox" id="pulse"> Pulse top-30 (decorative)</label>
      </div>
    </section>

    <section>
      <label>Color mode</label>
      <select id="color-mode">
        <option value="in_pool" selected>In-pool intensity (heat)</option>
        <option value="sector">Sector</option>
        <option value="tier">Tier</option>
        <option value="role">Role</option>
      </select>
    </section>

    <section>
      <label>Search</label>
      <input type="text" id="search" placeholder="@handle…">
    </section>

    <section>
      <label>View</label>
      <button class="btn" id="fit">Fit</button>
      <button class="btn" id="reset">Reset</button>
      <button class="btn" id="freeze">Freeze</button>
    </section>

    <section>
      <label>Visible</label>
      <div class="stat-row"><span>Nodes</span><span id="stat-nodes">0</span></div>
      <div class="stat-row"><span>Edges</span><span id="stat-edges">0</span></div>
      <div class="stat-row"><span>Candidates</span><span id="stat-cand">0</span></div>
      <div class="stat-row"><span>Cohort</span><span id="stat-cohort">0</span></div>
      <div class="stat-row"><span>Kingmakers</span><span id="stat-king">0</span></div>
    </section>

    <div class="footer">
      Click a node to highlight neighbors · Shift+click opens <code>x.com/&lt;handle&gt;</code><br>
      Drag to pan · scroll to zoom · spread changes spring length
    </div>
  </div>

  <div id="network"></div>
  <div id="caption">
    <div class="title">SolStitch follow-graph — kingmakers among the SolStitch-adjacent KOL ecosystem</div>
    Each dot is one X account. Size encodes <i>log<sub>10</sub>(follower count)</i>;
    color encodes <i>in-pool</i> — the count of __COHORT_SIZE__ surveyed cohort KOLs
    who follow this account. Outreach-plan candidates and the surveyed cohort
    have colored borders.
    <div class="meta">
      Sources: SableKOL bank · Phase 2 Doji + 9dcc + Fabricant audience extraction ·
      Phase 6 + 6b cohort following pulls (66 KOLs, completed runs only). Generated __GENERATED_DATE__.
    </div>
  </div>
</div>
<div id="help-overlay">
  <h2>Quick reference</h2>
  <ul style="padding-left: 18px; margin: 0;">
    <li><b>Network size</b>: top-N nodes by importance score (in-pool count + sector bonus). Candidates &amp; cohort always retained.</li>
    <li><b>Roles</b>: combine freely. Empty = no nodes.</li>
    <li><b>Distance</b>: physics spring length. Higher = more spread.</li>
    <li><b>Node size</b>: multiplies the log10(followers) base size. Higher = more dramatic.</li>
    <li><b>Pulse</b>: top-30 most-connected oscillate, amplitude ∝ in-pool count.</li>
    <li><b>Click a node</b>: highlights neighbors. <b>Shift+click</b>: opens <code>x.com/&lt;handle&gt;</code>.</li>
    <li><b>Search</b>: incremental match by handle. First hit jumps + selects.</li>
    <li><b>Freeze</b>: stops physics so you can pan/inspect without drift.</li>
  </ul>
  <div style="margin-top:8px; color:var(--muted); font-size:11px">click "help" again to dismiss</div>
</div>

<script>
const ALL_NODES = __NODES__;
const ALL_EDGES = __EDGES__;
const ALL_SECTORS = __SECTORS__;
const MAX_IN_POOL = __MAX_IN_POOL__;

// Slider buckets — top-N caps. Candidates+cohort are always included via
// score offset; the cap determines how many kingmakers get added on top.
// Default lands at "Less" (400) per Tufte's shrink principle — denser
// visuals are harder to read; small + clearly labeled beats a hairball.
const SIZE_BUCKETS = [150, 250, 400, 700, 1500, ALL_NODES.length];
const BUCKET_LABELS = ['Least','Lesser','Less','Some','More','Most'];

// ---- Visual mappers ----
function colorForInPool(p) {
  // Tufte-light heat scale: warm-grey (visible on off-white bg) → red.
  // sqrt-scaled so the long tail of low values doesn't all collapse to the
  // floor color.
  const t = Math.sqrt(Math.max(0, p) / Math.max(MAX_IN_POOL, 1));
  const r = Math.round(218 - (218 - 178) * t);
  const g = Math.round(216 - (216 -  34) * t);
  const b = Math.round(208 - (208 -  34) * t);
  return `rgb(${r},${g},${b})`;
}
const SECTOR_PALETTE = {
  fashion: '#e377c2', culture: '#ff9896', art: '#9467bd', design: '#c49c94',
  nfts: '#ff7f0e', streetwear: '#bcbd22', music: '#17becf', social: '#aec7e8',
  creator: '#f7b6d2', media: '#dbdb8d', defi: '#7f7f7f', gaming: '#8c564b',
  ai: '#e7ba52', infra: '#393b79', other: '#555', l2_eth: '#637939',
  btc: '#e7969c', memes: '#9c9ede', desci: '#c5b0d5'
};
const TIER_COLOR = { A: '#d62728', B: '#ff7f0e', C: '#bcbd22', unranked: '#555' };
const ROLE_COLOR = {
  'candidate+cohort': '#9467bd',
  'candidate+kingmaker': '#1f77b4',
  'candidate': '#1f77b4',
  'cohort': '#2ca02c',
  'kingmaker': '#d62728',
  'other': '#555',
};
function colorForNode(n, mode) {
  switch (mode) {
    case 'sector': return SECTOR_PALETTE[n.sector] || '#555';
    case 'tier': return TIER_COLOR[n.tier || 'unranked'];
    case 'role': return ROLE_COLOR[n.role] || '#555';
    case 'in_pool':
    default: return colorForInPool(n.in_pool);
  }
}
function borderForNode(n) {
  // Tufte-light: borders stay subtle — they encode role/tier hierarchy but
  // shouldn't shout. The hue (blue/green) does the categorical work; the
  // width carries the tier emphasis only inside candidates.
  if (n.role.includes('candidate')) {
    if (n.tier === 'A') return { color: '#2c5282', width: 2.5 };
    if (n.tier === 'B') return { color: '#2c5282', width: 1.5 };
    return { color: '#2c5282', width: 1 };
  }
  if (n.role.includes('cohort')) return { color: '#2d6a4f', width: 1.5 };
  return { color: '#a0a098', width: 0.4 };
}
function baseSize(followers, scale) {
  const f = Math.max(followers, 0);
  if (f <= 0) return 4 * scale;
  // More dramatic: 4 + 12 * log10. At 10K → 52, 100K → 64, 1M → 76, 10M → 88.
  // Scale slider multiplies on top so the user can crank further.
  return Math.max(4, Math.min(140, 4 + 12 * Math.log10(Math.max(f, 10)))) * scale;
}

// ---- State ----
const state = {
  topN: SIZE_BUCKETS[2],   // default to "Less" (400)
  showCand: true, showCohort: true, showKing: true,
  showOrgs: false,         // PEOPLE only by default — orgs ignore DMs
  showCelebs: false,       // Vitalik/CZ/Elon class — broadcast accounts
  tiers: new Set(['A','B','C','unranked']),
  sectors: new Set(),  // empty = all
  minPool: 0, minFollowersExp: 0,
  spring: 200, sizeScale: 1.5,
  colorMode: 'in_pool', pulse: false, frozen: false,
};

// ---- Pre-populate sector dropdown ----
const sectorSel = document.getElementById('sector-filter');
for (const s of ALL_SECTORS) {
  const opt = document.createElement('option');
  opt.value = s; opt.textContent = s;
  sectorSel.appendChild(opt);
}

// ---- vis.js network ----
const nodesDS = new vis.DataSet();
const edgesDS = new vis.DataSet();
const network = new vis.Network(
  document.getElementById('network'),
  { nodes: nodesDS, edges: edgesDS },
  {
    nodes: {
      shape: 'dot',
      // Light Tufte palette — labels in dark serif, no border halo by default.
      font: { color: '#1a1a18', size: 11, face: 'Georgia, "Times New Roman", serif',
              strokeWidth: 3, strokeColor: '#fafaf7' }
    },
    edges: {
      // Edges nearly invisible at rest. Tufte: drop chartjunk; let position +
      // size + color carry the message. Selection brightens neighbors below.
      arrows: { to: { enabled: false } },
      color: { color: '#a8a8a0', opacity: 0.06, highlight: '#b22222', hover: '#b22222' },
      width: 0.4, smooth: false, hoverWidth: 1.2, selectionWidth: 1.2,
    },
    physics: {
      solver: 'forceAtlas2Based',
      forceAtlas2Based: {
        gravitationalConstant: -80,    // stronger repulsion → less hairball
        centralGravity: 0.003,         // weaker pull-to-center
        springLength: 200,             // matches state.spring default
        springConstant: 0.04,
        damping: 0.7,
        avoidOverlap: 1,               // hard constraint — KEY FIX for "nodes on top of each other"
      },
      stabilization: { iterations: 300, fit: true, updateInterval: 25 },
      maxVelocity: 30,
      minVelocity: 0.5,
      timestep: 0.5,
    },
    interaction: { hover: true, tooltipDelay: 80, hideEdgesOnDrag: true,
                   navigationButtons: false, keyboard: true }
  }
);

// Track stabilized state so the spread slider knows when to wake physics.
let physicsRunning = true;
network.on('stabilizationIterationsDone', () => { physicsRunning = false; });

// ---- Filter + render ----
const idIndex = new Map(ALL_NODES.map(n => [n.id, n]));
const baseSizeById = new Map();
const visibleSet = new Set();
let topPulseIds = [];

function pickVisibleIds() {
  // 1. Apply org + celeb filters FIRST so the top-N cap counts the
  //    actually-outreachable pool (people, not orgs, not broadcast whales).
  let pool = ALL_NODES;
  if (!state.showOrgs)   pool = pool.filter(n => !n.is_org);
  if (!state.showCelebs) pool = pool.filter(n => !n.is_celeb);
  // 2. Cap by top-N (now over the filtered pool).
  const top = pool.slice(0, state.topN);
  // 3. Apply role / tier / sector / numeric filters.
  const minF = Math.pow(10, state.minFollowersExp);
  const out = [];
  for (const n of top) {
    if (!state.showCand && n.role.includes('candidate')) continue;
    if (!state.showCohort && n.role.includes('cohort')) continue;
    const isPureKing = (n.role === 'kingmaker');
    if (!state.showKing && isPureKing) continue;
    if (n.role.includes('candidate')) {
      const t = n.tier || 'unranked';
      if (!state.tiers.has(t)) continue;
    }
    if (state.sectors.size > 0 && n.sector && !state.sectors.has(n.sector)) continue;
    if (n.in_pool < state.minPool) continue;
    if (n.followers < minF) continue;
    out.push(n);
  }
  return out;
}

// ---- Radial seed: start nodes in informative positions even before
// physics stabilizes. Cohort in inner ring, candidates middle, kingmakers
// outer. Tufte's layering: hierarchy of importance → spatial hierarchy.
function radialSeed(n, idx, total) {
  const ring = n.role.includes('cohort') ? 0
             : n.role.includes('candidate') ? 1 : 2;
  const radii = [120, 280, 520];
  const angle = (idx / Math.max(total, 1)) * Math.PI * 2;
  // Add a small jitter so coincident-role nodes don't stack.
  const jitter = ((n.id.charCodeAt(0) || 0) % 50) - 25;
  return { x: Math.cos(angle) * (radii[ring] + jitter),
           y: Math.sin(angle) * (radii[ring] + jitter) };
}

function rebuild() {
  const visible = pickVisibleIds();
  visibleSet.clear();
  for (const n of visible) visibleSet.add(n.id);

  // Build vis.js node objects.
  const newNodes = visible.map((n, idx) => {
    const sz = baseSize(n.followers, state.sizeScale);
    baseSizeById.set(n.id, sz);
    const border = borderForNode(n);
    // Direct labels: every visible candidate/cohort always; kingmakers only
    // when they're structurally important. Tufte: label thoroughly, but
    // crowding kills.
    const showLabel = n.role.includes('candidate') || n.role.includes('cohort')
                      || n.in_pool >= Math.max(8, MAX_IN_POOL * 0.4)
                      || n.followers >= 200000;
    const seed = radialSeed(n, idx, visible.length);
    return {
      id: n.id,
      label: showLabel ? n.id : '',
      size: sz,
      x: seed.x, y: seed.y,
      color: { background: colorForNode(n, state.colorMode), border: border.color },
      borderWidth: border.width,
      title: `@${n.id}\nfollowers: ${n.followers.toLocaleString()}\nin_pool: ${n.in_pool} of __COHORT_SIZE__\nrole: ${n.role}\ntier: ${n.tier || '—'}\nsector: ${n.sector || '—'}\narchetype: ${n.archetype || '—'}\n\n[shift+click → x.com/${n.id}]`,
    };
  });

  // Edges restricted to visible endpoints. 33K edges → fast in JS.
  const newEdges = [];
  let edgeId = 0;
  for (const e of ALL_EDGES) {
    if (visibleSet.has(e.from) && visibleSet.has(e.to)) {
      newEdges.push({ id: edgeId++, from: e.from, to: e.to });
    }
  }

  nodesDS.clear(); edgesDS.clear();
  nodesDS.add(newNodes); edgesDS.add(newEdges);

  // Top-30 most-connected for pulsing
  topPulseIds = visible.slice().sort((a, b) => b.in_pool - a.in_pool).slice(0, 30).map(n => n.id);

  // Update stats
  document.getElementById('stat-nodes').textContent = visible.length.toLocaleString();
  document.getElementById('stat-edges').textContent = newEdges.length.toLocaleString();
  document.getElementById('stat-cand').textContent =
    visible.filter(n => n.role.includes('candidate')).length;
  document.getElementById('stat-cohort').textContent =
    visible.filter(n => n.role.includes('cohort')).length;
  document.getElementById('stat-king').textContent =
    visible.filter(n => n.in_pool >= 4).length;
}

// ---- Pulse animation (top-30 by in_pool, amplitude ∝ in_pool) ----
function pulseTick() {
  if (state.pulse && !state.frozen) {
    const t = performance.now() / 380;
    const updates = [];
    for (let i = 0; i < topPulseIds.length; i++) {
      const id = topPulseIds[i];
      const n = idIndex.get(id);
      if (!n) continue;
      const base = baseSizeById.get(id) || 8;
      // Heavier pulse for higher in_pool (up to ±30%)
      const amp = 0.05 + Math.min(0.30, n.in_pool / Math.max(MAX_IN_POOL, 1) * 0.30);
      const pulse = 1 + amp * Math.sin(t + i * 0.4);
      updates.push({ id, size: base * pulse });
    }
    if (updates.length) nodesDS.update(updates);
  }
  requestAnimationFrame(pulseTick);
}
requestAnimationFrame(pulseTick);

// ---- Wire up controls ----
const $ = id => document.getElementById(id);

$('size-slider').addEventListener('input', e => {
  const idx = parseInt(e.target.value, 10);
  state.topN = SIZE_BUCKETS[idx];
  document.querySelectorAll('.size-buckets span').forEach(s =>
    s.classList.toggle('active', parseInt(s.dataset.bucket, 10) === idx));
  rebuild();
});
$('r-cand').addEventListener('change', e => { state.showCand = e.target.checked; rebuild(); });
$('r-cohort').addEventListener('change', e => { state.showCohort = e.target.checked; rebuild(); });
$('r-king').addEventListener('change', e => { state.showKing = e.target.checked; rebuild(); });
$('r-orgs').addEventListener('change', e => { state.showOrgs = e.target.checked; rebuild(); });
$('r-celebs').addEventListener('change', e => { state.showCelebs = e.target.checked; rebuild(); });
document.querySelectorAll('.t-filter').forEach(cb => {
  cb.addEventListener('change', () => {
    state.tiers.clear();
    document.querySelectorAll('.t-filter').forEach(c => { if (c.checked) state.tiers.add(c.value); });
    rebuild();
  });
});
sectorSel.addEventListener('change', () => {
  state.sectors = new Set(Array.from(sectorSel.selectedOptions).map(o => o.value));
  rebuild();
});
$('min-pool').addEventListener('input', e => {
  state.minPool = parseInt(e.target.value, 10);
  $('min-pool-val').textContent = state.minPool;
  rebuild();
});
$('min-followers').addEventListener('input', e => {
  state.minFollowersExp = parseFloat(e.target.value);
  const f = Math.pow(10, state.minFollowersExp);
  $('min-followers-val').textContent = f >= 1000 ? Math.round(f).toLocaleString() : Math.round(f);
  rebuild();
});
// Spread slider: vis.js's springLength only takes effect during active
// physics. After stabilization, physics auto-stops so the slider would
// appear dead past ~halfway (operator-reported bug). Fix: wake physics
// briefly on each change so the change actually propagates.
let springWakeTimer = null;
$('spring').addEventListener('input', e => {
  state.spring = parseInt(e.target.value, 10);
  $('spring-val').textContent = state.spring;
  network.setOptions({ physics: { enabled: true,
    forceAtlas2Based: { springLength: state.spring } } });
  physicsRunning = true;
  if (springWakeTimer) clearTimeout(springWakeTimer);
  // After ~600ms of no slider movement, freeze again so the layout settles.
  springWakeTimer = setTimeout(() => {
    if (!state.frozen) {
      network.stopSimulation();
      network.setOptions({ physics: { enabled: false } });
      physicsRunning = false;
    }
  }, 600);
});
$('size-scale').addEventListener('input', e => {
  state.sizeScale = parseInt(e.target.value, 10) / 100;
  $('size-scale-val').textContent = state.sizeScale.toFixed(2) + '×';
  rebuild();
});
$('pulse').addEventListener('change', e => { state.pulse = e.target.checked; });
$('color-mode').addEventListener('change', e => {
  state.colorMode = e.target.value;
  rebuild();
});
$('search').addEventListener('input', e => {
  const q = e.target.value.replace(/^@/, '').toLowerCase();
  if (!q) { network.unselectAll(); return; }
  const hit = ALL_NODES.find(n => n.id.startsWith(q) && visibleSet.has(n.id));
  if (hit) {
    network.selectNodes([hit.id]);
    network.focus(hit.id, { scale: 1.5, animation: { duration: 350 } });
  }
});
$('fit').addEventListener('click', () => network.fit({ animation: { duration: 400 } }));
function applyPreset(preset) {
  // Helpers
  const setSlider = (id, value, valueId, displayFn) => {
    $(id).value = value;
    if (valueId) $(valueId).textContent = displayFn ? displayFn(value) : value;
  };
  const setSizeBucket = (idx) => {
    $('size-slider').value = idx; state.topN = SIZE_BUCKETS[idx];
    document.querySelectorAll('.size-buckets span').forEach(s =>
      s.classList.toggle('active', parseInt(s.dataset.bucket, 10) === idx));
  };
  // Reset to default state first
  ['r-cand','r-cohort','r-king'].forEach(id => { $(id).checked = true; });
  state.showCand = state.showCohort = state.showKing = true;
  $('r-orgs').checked = false; state.showOrgs = false;
  $('r-celebs').checked = false; state.showCelebs = false;
  document.querySelectorAll('.t-filter').forEach(c => { c.checked = true; });
  state.tiers = new Set(['A','B','C','unranked']);
  Array.from(sectorSel.options).forEach(o => o.selected = false);
  state.sectors = new Set();
  setSlider('min-pool', 0, 'min-pool-val'); state.minPool = 0;
  setSlider('min-followers', 0, 'min-followers-val', () => '0'); state.minFollowersExp = 0;
  setSlider('size-scale', 150, 'size-scale-val', v => (v/100).toFixed(2) + '×');
  state.sizeScale = 1.5;
  $('color-mode').value = 'in_pool'; state.colorMode = 'in_pool';
  $('search').value = '';
  setSizeBucket(2);

  switch (preset) {
    case 'fashion':
      // Top fashion-cluster kingmakers
      ['fashion','culture','art','design','streetwear'].forEach(s => {
        const opt = Array.from(sectorSel.options).find(o => o.value === s);
        if (opt) opt.selected = true;
      });
      state.sectors = new Set(['fashion','culture','art','design','streetwear']);
      setSlider('min-pool', 4, 'min-pool-val'); state.minPool = 4;
      setSizeBucket(3);
      $('color-mode').value = 'in_pool'; state.colorMode = 'in_pool';
      break;
    case 'hidden':
      // Sub-10K-followers but high in-pool — operator's "must-introduce" candidates
      setSlider('min-pool', 6, 'min-pool-val'); state.minPool = 6;
      setSlider('min-followers', 0, 'min-followers-val', () => '0'); state.minFollowersExp = 0;
      // Cap at <10K via the max-followers — currently no max; emulate by raising size-scale
      // and using color tier; we'll filter post-rebuild via min-followers=0 + manual review.
      // Toggle off generic candidates that exceed 10K via tier filter below.
      setSizeBucket(3);
      $('color-mode').value = 'tier'; state.colorMode = 'tier';
      break;
    case 'tier_a':
      ['r-cohort','r-king'].forEach(id => { $(id).checked = false; });
      state.showCohort = false; state.showKing = false;
      document.querySelectorAll('.t-filter').forEach(c => {
        c.checked = (c.value === 'A'); });
      state.tiers = new Set(['A']);
      setSizeBucket(5);  // show all candidates regardless of cap
      break;
    case 'default':
    default:
      // Already reset above
      break;
  }
  rebuild();
}

document.querySelectorAll('.btn[data-preset]').forEach(b => {
  b.addEventListener('click', () => applyPreset(b.dataset.preset));
});

$('reset').addEventListener('click', () => applyPreset('default'));
$('freeze').addEventListener('click', () => {
  state.frozen = !state.frozen;
  network.setOptions({ physics: { enabled: !state.frozen } });
  $('freeze').textContent = state.frozen ? 'Unfreeze layout' : 'Freeze layout';
});

// ---- Click handling: shift+click opens X profile ----
network.on('click', params => {
  const evt = params.event && params.event.srcEvent;
  if (params.nodes.length > 0 && evt && evt.shiftKey) {
    window.open(`https://x.com/${params.nodes[0]}`, '_blank', 'noopener');
    return;
  }
  // Plain click: highlight neighbors (vis.js default selection already does
  // a soft highlight; we also dim non-neighbors)
  if (params.nodes.length > 0) {
    const focusId = params.nodes[0];
    const neighbors = new Set([focusId]);
    edgesDS.forEach(e => {
      if (e.from === focusId) neighbors.add(e.to);
      if (e.to === focusId) neighbors.add(e.from);
    });
    const dim = [];
    nodesDS.forEach(n => {
      if (!neighbors.has(n.id)) {
        dim.push({ id: n.id, opacity: 0.18 });
      } else {
        dim.push({ id: n.id, opacity: 1 });
      }
    });
    nodesDS.update(dim);
  } else {
    const reset = [];
    nodesDS.forEach(n => reset.push({ id: n.id, opacity: 1 }));
    nodesDS.update(reset);
  }
});

// Help toggle
const help = $('help-overlay');
$('help-toggle').addEventListener('click', () => {
  help.style.display = help.style.display === 'block' ? 'none' : 'block';
});

// ---- Boot ----
rebuild();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
