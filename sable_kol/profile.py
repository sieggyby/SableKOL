"""Project profile builders.

Two paths:

* **Path (i)** — ``build_org_profile(conn, org_id)`` reads sable.db for the
  given Sable client/prospect:

    - ``orgs.config_json`` → sector, stage
    - ``orgs.twitter_handle`` → handle to use for voice doc lookup
    - top ``entity_tags`` across the org's entities → community shape
    - voice docs at ``~/.sable/profiles/@<handle>/{tone,interests,context,notes}.md``,
      concat'd as ``project_voice_blob``. Missing voice docs degrade gracefully.

* **Path (ii)** — ``build_external_profile(...)`` reads/writes
  ``project_profiles_external`` for a non-onboarded handle:

    - Default ``manual_only``: operator supplies ``--sector`` and optionally
      ``--themes``. No SocialData call.
    - ``--paid-enrich``: one SocialData ``GET /twitter/user/{handle}`` call.
      TTL = 7 days from ``last_enriched_at``. ``--refresh-paid`` forces refresh.
      Logs a ``cost_events`` row.

The returned ``Profile`` is the matcher's input.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from sable_kol import cost as cost_mod
from sable_kol.db import (
    ExternalProfile,
    get_external_profile,
    mark_external_profile_used,
    normalize_handle,
    upsert_external_profile,
)


# ---------------------------------------------------------------------------
# Profile dataclass
# ---------------------------------------------------------------------------

@dataclass
class Profile:
    """Common project profile shape for both paths."""

    source: str  # "org" | "external_manual" | "external_paid_basic"
    org_id: str | None = None
    handle: str | None = None
    sector: str | None = None
    stage: str | None = None
    sectors: list[str] = field(default_factory=list)
    themes: list[str] = field(default_factory=list)
    top_tags: list[str] = field(default_factory=list)
    voice_blob: str | None = None

    def to_evidence_dict(self) -> dict:
        """Compact view used in Haiku prompts."""
        return {
            "source": self.source,
            "org_id": self.org_id,
            "handle": self.handle,
            "sector": self.sector,
            "stage": self.stage,
            "sectors": self.sectors,
            "themes": self.themes,
            "top_tags": self.top_tags,
            # voice_blob can be long — included separately by callers
        }


# ---------------------------------------------------------------------------
# Voice doc reading
# ---------------------------------------------------------------------------

VOICE_DOC_FILES = ["tone.md", "interests.md", "context.md", "notes.md"]


def _voice_blob_path(handle: str) -> Path:
    """``~/.sable/profiles/@<handle>/`` per Slopper convention."""
    home = Path(os.environ.get("SABLE_HOME") or (Path.home() / ".sable"))
    return home / "profiles" / f"@{handle}"


def read_voice_blob(handle: str) -> str | None:
    """Concatenate the four voice doc files for a handle. None if dir absent."""
    base = _voice_blob_path(handle)
    if not base.exists():
        return None
    parts: list[str] = []
    for fname in VOICE_DOC_FILES:
        p = base / fname
        if p.exists():
            parts.append(f"## {fname}\n{p.read_text(encoding='utf-8')}")
    if not parts:
        return None
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Path (i) — Sable org
# ---------------------------------------------------------------------------

def build_org_profile(conn: Any, org_id: str) -> Profile:
    """Path (i): assemble a Profile from sable.db + voice docs."""
    org_row = conn.execute(
        "SELECT org_id, twitter_handle, config_json FROM orgs WHERE org_id = :id",
        {"id": org_id},
    ).fetchone()
    if org_row is None:
        raise LookupError(f"No org with org_id={org_id!r}")
    config = json.loads(org_row["config_json"] or "{}")

    sector = config.get("sector")
    stage = config.get("stage")
    sectors = [sector] if sector else []
    themes = config.get("themes") or []

    # Top entity_tags across this org's entities (most-used first).
    top_tags = [
        r["tag"]
        for r in conn.execute(
            "SELECT t.tag AS tag, COUNT(*) AS n "
            "FROM entity_tags t JOIN entities e ON e.entity_id = t.entity_id "
            "WHERE e.org_id = :oid AND t.is_current = 1 "
            "GROUP BY t.tag ORDER BY n DESC LIMIT 15",
            {"oid": org_id},
        ).fetchall()
    ]

    voice_blob = None
    twitter_handle = org_row["twitter_handle"]
    if twitter_handle:
        voice_blob = read_voice_blob(normalize_handle(twitter_handle))

    return Profile(
        source="org",
        org_id=org_id,
        handle=twitter_handle,
        sector=sector,
        stage=stage,
        sectors=sectors,
        themes=themes,
        top_tags=top_tags,
        voice_blob=voice_blob,
    )


# ---------------------------------------------------------------------------
# Path (ii) — External handle
# ---------------------------------------------------------------------------

# 7 days, per Sable's SocialData guidance.
PAID_PROFILE_TTL_SECONDS = 7 * 24 * 60 * 60


SocialDataFetcher = Callable[[str], dict]
"""Function that fetches a profile dict from SocialData given a handle."""


def _is_stale(last_enriched_at: str | None, ttl_seconds: int) -> bool:
    if not last_enriched_at:
        return True
    try:
        dt = datetime.fromisoformat(last_enriched_at.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    age = (datetime.now(timezone.utc) - dt).total_seconds()
    return age > ttl_seconds


def build_external_profile(
    conn: Any,
    *,
    handle: str,
    sector: str,
    themes: list[str] | None = None,
    paid_enrich: bool = False,
    refresh_paid: bool = False,
    socialdata_fetcher: SocialDataFetcher | None = None,
) -> Profile:
    """Path (ii): cache or build a lite project profile for an external handle.

    ``socialdata_fetcher`` is injectable for tests. In production it points to
    Slopper's ``socialdata_get`` call wrapped to fetch ``GET /twitter/user/{handle}``.
    """
    h = normalize_handle(handle)
    themes = themes or []
    cached = get_external_profile(conn, h)

    do_paid_call = False
    if paid_enrich:
        if cached is None:
            do_paid_call = True
        elif refresh_paid:
            do_paid_call = True
        elif cached.enrichment_source != "paid_basic":
            do_paid_call = True
        elif _is_stale(cached.last_enriched_at, PAID_PROFILE_TTL_SECONDS):
            do_paid_call = True

    if do_paid_call:
        if socialdata_fetcher is None:
            socialdata_fetcher = _default_socialdata_fetcher
        try:
            data = socialdata_fetcher(h)
        except Exception:
            cost_mod.record(
                conn,
                org_id=None,
                call_type="socialdata_user_profile",
                cost_usd=0.002,
                call_status="error",
            )
            raise
        bio = data.get("description") or data.get("bio") or ""
        followers = data.get("followers_count") or data.get("followers")
        twitter_id = (
            data.get("id_str")
            or (str(data["id"]) if data.get("id") is not None else None)
        )
        profile_blob = (
            f"# {data.get('name') or h}\n"
            f"@{h}\n"
            f"followers: {followers}\n"
            f"verified: {data.get('verified', False)}\n\n"
            f"{bio}\n"
        )
        upsert_external_profile(
            conn,
            handle=h,
            sector_tags=[sector] if sector else [],
            themes=themes,
            profile_blob=profile_blob,
            enrichment_source="paid_basic",
            twitter_id=twitter_id,
            mark_enriched_now=True,
        )
        cost_mod.record(
            conn,
            org_id=None,
            call_type="socialdata_user_profile",
            cost_usd=0.002,
        )
        cached = get_external_profile(conn, h)
        source = "external_paid_basic"
    else:
        # No paid call. If no cached row, write a manual_only one.
        if cached is None:
            upsert_external_profile(
                conn,
                handle=h,
                sector_tags=[sector] if sector else [],
                themes=themes,
                profile_blob=None,
                enrichment_source="manual_only",
            )
            cached = get_external_profile(conn, h)
        else:
            # Update sector / themes if operator changed them.
            upsert_external_profile(
                conn,
                handle=h,
                sector_tags=cached.sector_tags or ([sector] if sector else []),
                themes=themes or cached.themes,
                profile_blob=cached.profile_blob,
                enrichment_source=cached.enrichment_source,
                twitter_id=cached.twitter_id,
                mark_enriched_now=False,
            )
            cached = get_external_profile(conn, h)
        source = (
            "external_paid_basic"
            if cached.enrichment_source == "paid_basic"
            else "external_manual"
        )

    mark_external_profile_used(conn, h)
    return Profile(
        source=source,
        handle=h,
        sector=sector,
        stage=None,
        sectors=[sector] if sector else [],
        themes=themes or cached.themes,
        top_tags=[],
        voice_blob=cached.profile_blob,
    )


def _default_socialdata_fetcher(handle: str) -> dict:
    """Production SocialData fetcher — requires Slopper installed."""
    try:
        from sable.shared.socialdata import socialdata_get  # type: ignore[import-not-found]
    except ImportError as e:
        raise RuntimeError(
            "--paid-enrich requires Slopper. "
            "Install with: pip install -e '.[paid-enrich]'"
        ) from e
    return socialdata_get(f"/twitter/user/{handle}")
