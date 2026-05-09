"""Cross-platform presence enrichment (Instagram, TikTok, Threads, YouTube etc).

Reads a JSON array of entries shaped like:

    {
      "handle": "<x_handle>",         # the Twitter/X handle we use as primary key
      "instagram": {"handle": str, "followers": int, "verified": bool|null},
      "tiktok":    {"handle": str, "followers": int, "verified": bool|null},
      "threads":   {"handle": str, "followers": int, "verified": bool|null},
      "youtube":   {"handle": str, "subscribers": int, "verified": bool|null},
      "notes":     "<optional cross-platform notes>"
    }

…and merges into ``kol_candidates.platform_presence_json`` per matching X handle.
Unknown platforms are accepted (forward-compat) but a warning is printed.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sable_kol.db import open_db, normalize_handle
from sable_kol.grok_import import parse_grok_json


logger = logging.getLogger(__name__)

# Platforms we recognize. Forward-compatible — extend as needed.
KNOWN_PLATFORMS = {"instagram", "tiktok", "threads", "youtube", "substack", "lens", "farcaster"}

# Per-platform follower-count field names. YouTube uses "subscribers" colloquially.
FOLLOWER_KEY = {
    "instagram": "followers",
    "tiktok": "followers",
    "threads": "followers",
    "youtube": "subscribers",
    "substack": "subscribers",
    "lens": "followers",
    "farcaster": "followers",
}


@dataclass
class CrossPlatformSummary:
    parsed: int = 0
    updated: int = 0
    not_found: int = 0
    platforms_updated: int = 0


def _normalize_platform_entry(name: str, raw: dict) -> dict | None:
    """Coerce a per-platform dict into the canonical shape. Returns None if invalid."""
    if not isinstance(raw, dict):
        return None
    handle = raw.get("handle")
    if not handle or not isinstance(handle, str):
        return None
    fol_key = FOLLOWER_KEY.get(name, "followers")
    fol = raw.get(fol_key)
    if fol is not None:
        try:
            fol = int(fol)
        except (TypeError, ValueError):
            fol = None
    out = {
        "handle": handle.strip().lstrip("@"),
        fol_key: fol,
        "verified": bool(raw.get("verified")) if raw.get("verified") is not None else None,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    # Carry through any extra keys for forward-compat
    for k, v in raw.items():
        if k not in {"handle", fol_key, "verified"} and k not in out:
            out[k] = v
    return out


def run_cross_platform_import(path: str | Path) -> CrossPlatformSummary:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    entries = parse_grok_json(text)  # tolerates truncation + markdown fences
    summary = CrossPlatformSummary(parsed=len(entries))

    with open_db() as conn:
        for entry in entries:
            handle = entry.get("handle")
            if not handle:
                continue
            h = normalize_handle(str(handle))
            row = conn.execute(
                "SELECT candidate_id, platform_presence_json FROM kol_candidates "
                "WHERE handle_normalized = :h AND is_unresolved = 0",
                {"h": h},
            ).fetchone()
            if row is None:
                summary.not_found += 1
                continue
            current = json.loads(row["platform_presence_json"] or "{}")
            n_changed = 0
            for plat, data in entry.items():
                if plat == "handle" or plat == "notes":
                    continue
                if plat not in KNOWN_PLATFORMS:
                    logger.warning("unknown platform %r for @%s — accepting anyway", plat, h)
                if not isinstance(data, dict):
                    continue
                normalized = _normalize_platform_entry(plat, data)
                if normalized is None:
                    continue
                current[plat] = normalized
                n_changed += 1
            if n_changed == 0:
                continue
            conn.execute(
                "UPDATE kol_candidates SET platform_presence_json = :j, "
                "  last_enriched_at = CURRENT_TIMESTAMP "
                "WHERE candidate_id = :cid",
                {"j": json.dumps(current), "cid": row["candidate_id"]},
            )
            # Append cross-platform note if provided
            if entry.get("notes"):
                conn.execute(
                    "UPDATE kol_candidates SET manual_notes = COALESCE(manual_notes,'') || :n "
                    "WHERE candidate_id = :cid",
                    {"n": f" [cross-platform: {entry['notes']}]", "cid": row["candidate_id"]},
                )
            summary.updated += 1
            summary.platforms_updated += n_changed
        conn.commit()
    return summary


def get_platform_presence(conn: Any, handle_normalized: str) -> dict:
    """Read platform_presence_json for a candidate. Returns {} if missing."""
    row = conn.execute(
        "SELECT platform_presence_json FROM kol_candidates "
        "WHERE handle_normalized = :h AND is_unresolved = 0",
        {"h": handle_normalized},
    ).fetchone()
    if row is None or not row["platform_presence_json"]:
        return {}
    return json.loads(row["platform_presence_json"])
