"""Import enriched profile data from a Grok JSON response.

Grok returns objects of the shape defined in the prompt template
(``~/Downloads/grok_enrich_prompt.txt``). We parse with a tolerance for
truncated arrays — Grok often hits its output ceiling mid-batch — and
upsert each entry's fields into the matching ``kol_candidates`` row.

Field merge rules:
  bio_snapshot         <- Grok bio if non-null AND longer than current bio
  followers_snapshot   <- Grok followers if non-null
  verified             <- Grok verified if non-null (0/1)
  account_created_at   <- Grok account_created if non-null
  twitter_id           <- Grok twitter_id if non-null
  archetype_tags_json  <- merge Grok primary_archetype into existing tags (set union)
  sector_tags_json     <- merge Grok primary_sectors into existing tags (set union)
  status               <- 'dormant' if Grok is_active is explicitly false
  listed_count         <- Grok listed_count if non-null
  tweets_count         <- Grok tweets_count if non-null
  following_count      <- Grok following if non-null
  credibility_signal   <- Grok credibility_signal if non-null
  real_name_known      <- 1 if Grok real_name_known is true, 0 if false, leave alone if null
  notes                <- Grok notes if non-null AND non-empty
  last_enriched_at     <- CURRENT_TIMESTAMP if any field updated
  enrichment_tier      <- 'grok_basic' if any field updated

After updates, kol_strength_score is recomputed for every touched candidate.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sable_kol.db import open_db, normalize_handle
from sable_kol.enrich import compute_kol_strength


logger = logging.getLogger(__name__)

VALID_ARCHETYPES_GROK = {
    "thought_leader", "connector", "dev", "anon",
    "founder", "researcher", "ecosystem", "trader", "artist", "other",
}
VALID_SECTORS_GROK = {
    "defi", "gaming", "infra", "ai", "desci", "memes",
    "nfts", "l2_eth", "sol", "btc", "social",
    "art", "fashion", "design", "music", "culture", "other",
}
VALID_CREDIBILITY = {"high", "medium", "low", "unclear"}


@dataclass
class ImportSummary:
    parsed: int = 0
    updated: int = 0
    not_found: int = 0
    skipped_empty: int = 0
    rescored: int = 0


# ---------------------------------------------------------------------------
# Robust JSON parser (handles truncated arrays)
# ---------------------------------------------------------------------------

def parse_grok_json(text: str) -> list[dict]:
    """Parse Grok's JSON response, tolerating truncation.

    Grok responses often hit the output token ceiling mid-batch, leaving the
    final object incomplete. We strip markdown fences, try a clean parse, and
    fall back to ``json.JSONDecoder.raw_decode`` looping past commas to
    recover all fully-formed objects.
    """
    text = text.strip()
    # Strip markdown fences if present.
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    # Try the happy path first.
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return [d for d in data if isinstance(d, dict)]
        if isinstance(data, dict):
            return [data]
    except json.JSONDecodeError:
        pass

    # Recovery: walk objects with raw_decode.
    if text.startswith("["):
        text = text[1:]
    if text.endswith("]"):
        text = text[:-1]
    decoder = json.JSONDecoder()
    out: list[dict] = []
    pos = 0
    n = len(text)
    while pos < n:
        # Skip whitespace and commas.
        while pos < n and text[pos] in " \n\t\r,":
            pos += 1
        if pos >= n:
            break
        try:
            obj, end = decoder.raw_decode(text, pos)
        except json.JSONDecodeError:
            break  # truncated
        if isinstance(obj, dict):
            out.append(obj)
        pos = end
    return out


# ---------------------------------------------------------------------------
# Per-row update logic
# ---------------------------------------------------------------------------

def _bool_to_int(v: Any) -> int | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return 1 if v else 0
    return None


def _merge_tags(existing_json: str, new_tags: list, allowed: set) -> tuple[str, bool]:
    """Return (new_json, changed). Set-union with existing, filter to allowed."""
    if not new_tags or not isinstance(new_tags, list):
        return existing_json, False
    existing = json.loads(existing_json or "[]")
    merged = list(existing)
    changed = False
    for t in new_tags:
        if not isinstance(t, str):
            continue
        t = t.strip().lower()
        if t in allowed and t not in merged:
            merged.append(t)
            changed = True
    return json.dumps(merged), changed


def _apply_one(conn: Any, entry: dict, summary: ImportSummary) -> bool:
    """Update a single candidate row from a Grok entry. Returns True if updated."""
    handle = entry.get("handle")
    if not handle:
        summary.skipped_empty += 1
        return False
    h = normalize_handle(str(handle))
    row = conn.execute(
        "SELECT * FROM kol_candidates "
        "WHERE handle_normalized = :h AND is_unresolved = 0",
        {"h": h},
    ).fetchone()
    if row is None:
        summary.not_found += 1
        return False

    updates: dict[str, Any] = {}

    # Bio: prefer Grok's if non-null and longer than current.
    grok_bio = entry.get("bio")
    if grok_bio and isinstance(grok_bio, str):
        cur = row["bio_snapshot"] or ""
        if len(grok_bio) > len(cur):
            updates["bio_snapshot"] = grok_bio

    # Numeric direct-pass fields (only when non-null).
    for grok_key, db_col in [
        ("followers", "followers_snapshot"),
        ("twitter_id", "twitter_id"),
        ("account_created", "account_created_at"),
        ("listed_count", "listed_count"),
        ("tweets_count", "tweets_count"),
        ("following", "following_count"),
        ("credibility_signal", "credibility_signal"),
        ("location", "location"),
    ]:
        v = entry.get(grok_key)
        if v is None:
            continue
        if grok_key == "credibility_signal" and v not in VALID_CREDIBILITY:
            continue
        # Coerce twitter_id to string (some sources send as int).
        if grok_key == "twitter_id":
            v = str(v)
        updates[db_col] = v

    # Booleans -> integer 0/1.
    grok_verified = _bool_to_int(entry.get("verified"))
    if grok_verified is not None:
        updates["verified"] = grok_verified
    grok_realname = _bool_to_int(entry.get("real_name_known"))
    if grok_realname is not None:
        updates["real_name_known"] = grok_realname

    # is_active=False -> status='dormant' (only when explicitly false).
    is_active = entry.get("is_active")
    if is_active is False:
        updates["status"] = "dormant"

    # Notes — only set if non-empty string.
    grok_notes = entry.get("notes")
    if isinstance(grok_notes, str) and grok_notes.strip():
        updates["notes"] = grok_notes.strip()

    # Tag merges — separate UPDATEs since they need set logic.
    arch_json, arch_changed = _merge_tags(
        row["archetype_tags_json"],
        [entry.get("primary_archetype")] if entry.get("primary_archetype") else [],
        VALID_ARCHETYPES_GROK,
    )
    if arch_changed:
        updates["archetype_tags_json"] = arch_json
    sect_json, sect_changed = _merge_tags(
        row["sector_tags_json"],
        entry.get("primary_sectors") or [],
        VALID_SECTORS_GROK,
    )
    if sect_changed:
        updates["sector_tags_json"] = sect_json

    if not updates:
        # Nothing materially new. Still mark enriched so we don't re-process.
        conn.execute(
            "UPDATE kol_candidates SET last_enriched_at = CURRENT_TIMESTAMP, "
            "  enrichment_tier = 'grok_basic' "
            "WHERE candidate_id = :cid",
            {"cid": row["candidate_id"]},
        )
        conn.commit()
        return False

    # Build the UPDATE.
    set_parts = ", ".join(f"{col} = :{col}" for col in updates.keys())
    set_parts += ", last_enriched_at = CURRENT_TIMESTAMP, enrichment_tier = 'grok_basic'"
    params = {**updates, "cid": row["candidate_id"]}
    conn.execute(
        f"UPDATE kol_candidates SET {set_parts} WHERE candidate_id = :cid",
        params,
    )
    conn.commit()
    summary.updated += 1
    return True


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def run_grok_import(path: str | Path) -> ImportSummary:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    entries = parse_grok_json(text)
    summary = ImportSummary(parsed=len(entries))
    if not entries:
        return summary

    with open_db() as conn:
        touched_ids: list[int] = []
        for entry in entries:
            handle = entry.get("handle")
            if not handle:
                summary.skipped_empty += 1
                continue
            h = normalize_handle(str(handle))
            row = conn.execute(
                "SELECT candidate_id FROM kol_candidates "
                "WHERE handle_normalized = :h AND is_unresolved = 0",
                {"h": h},
            ).fetchone()
            if row:
                touched_ids.append(row["candidate_id"])
            _apply_one(conn, entry, summary)

        # Recompute kol_strength_score for each touched candidate.
        from sable_kol.db import _row_to_candidate
        for cid in touched_ids:
            r = conn.execute(
                "SELECT * FROM kol_candidates WHERE candidate_id = :cid",
                {"cid": cid},
            ).fetchone()
            c = _row_to_candidate(r)
            score = compute_kol_strength(c)
            conn.execute(
                "UPDATE kol_candidates SET kol_strength_score = :s "
                "WHERE candidate_id = :cid",
                {"s": score, "cid": cid},
            )
            summary.rescored += 1
        conn.commit()

    return summary
