"""Per-client configuration loader.

Reads YAML config files at ``~/.sable/clients/<id>.yaml`` (operator laptop)
or ``/opt/sable/clients/<id>.yaml`` (Hetzner production). Either path is
checked; production wins if both exist.

The schema is the canonical source of truth for everything client-specific:
mode (stealth/public), themes for vibe-fit, audience handles for Phase 2
extraction, manual pins, denylist extras, network axes (semantic-layout
keyword sets), and tier thresholds.

See ``~/Projects/SableKOL/docs/sableweb_kol_build_plan.md`` for the full
schema reference and operational context.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


# Production path. Read-only mount inside Docker containers; operator-edited
# via SSH (no admin UI in v1 per build plan).
PROD_CLIENT_DIR = Path("/opt/sable/clients")

# Local development path on the operator's laptop.
LOCAL_CLIENT_DIR = Path.home() / ".sable" / "clients"


# ---------------------------------------------------------------------------
# Validation helpers (audit finding #9 — clientId path-traversal protection)
# ---------------------------------------------------------------------------

_CLIENT_ID_RE = re.compile(r"^[a-z0-9_-]{1,32}$")


class InvalidClientIdError(ValueError):
    """Raised when a client_id fails regex validation or escapes the base dir."""


def assert_client_id(client_id: str) -> None:
    """Validate that ``client_id`` is well-formed and safe.

    * Regex: ``^[a-z0-9_-]{1,32}$`` — rejects ``..``, slashes, dots, etc.
    * Caller is responsible for the path-resolution check (separate concern;
      handled in ``load_client_config`` below).

    Raises:
        InvalidClientIdError: on any failure.
    """
    if not isinstance(client_id, str) or not _CLIENT_ID_RE.match(client_id):
        raise InvalidClientIdError(
            f"client_id must match {_CLIENT_ID_RE.pattern!r} (got {client_id!r})"
        )


def discovered_client_ids() -> set[str]:
    """Return the set of client_ids discoverable on disk (production + local)."""
    out: set[str] = set()
    for base in (PROD_CLIENT_DIR, LOCAL_CLIENT_DIR):
        if not base.is_dir():
            continue
        for path in base.glob("*.yaml"):
            stem = path.stem
            if _CLIENT_ID_RE.match(stem):
                out.add(stem)
    return out


# ---------------------------------------------------------------------------
# Schema dataclasses
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class AudienceConfig:
    """One audience-extraction target. SolStitch has three (Doji, 9dcc, Fabricant)."""
    handle: str
    label: str             # e.g. 'doji_audience' — used as discovery_source prefix
    curator_weight: float = 1.0


@dataclass(slots=True)
class TierThreshold:
    """Per-tier filter floor/ceiling. Stealth and public modes use different sets."""
    max_followers: int | None = None
    min_followers: int | None = None
    min_brokers: int | None = None
    min_vibe: float | None = None


@dataclass(slots=True)
class TierThresholds:
    """Three tiers per mode (Top priority / Mid priority / Long tail)."""
    A: TierThreshold = field(default_factory=TierThreshold)
    B: TierThreshold = field(default_factory=TierThreshold)
    C: TierThreshold = field(default_factory=TierThreshold)


@dataclass(slots=True)
class AxisConfig:
    """One semantic axis on the 2D network layout.

    Per /grill-me Q5: scoring is count-of-matched-tokens / saturation, with
    optional per-archetype boost.
    """
    label: str                                 # human-readable axis label
    keywords: list[str] = field(default_factory=list)
    saturation: int = 4                        # match count where score saturates at 1.0
    archetype_boosts: dict[str, float] = field(default_factory=dict)


@dataclass(slots=True)
class NetworkAxes:
    """Two axes for the SolStitch-style semantic layout."""
    x: AxisConfig
    y: AxisConfig


@dataclass(slots=True)
class ClientConfig:
    """Top-level client config. Loaded once at script startup."""
    client_id: str
    display_name: str
    mode: str                                  # 'stealth' | 'public'
    debut_date: str | None
    sector_focus: list[str]
    themes: list[str]
    audiences: list[AudienceConfig]
    manual_pins: list[str]
    org_denylist_extras: list[str]
    person_allowlist_extras: list[str]
    celebrity_denylist_extras: list[str]
    network_axes: NetworkAxes
    tier_thresholds: dict[str, TierThresholds]  # {'stealth': ..., 'public': ...}
    raw: dict[str, Any]                         # full raw dict for forward-compat


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def _resolve_config_path(client_id: str) -> Path:
    """Return the YAML path for ``client_id``, preferring production over local.

    Raises:
        InvalidClientIdError: if client_id fails validation.
        FileNotFoundError: if no config file exists in either base dir.
    """
    assert_client_id(client_id)
    for base in (PROD_CLIENT_DIR, LOCAL_CLIENT_DIR):
        candidate = base / f"{client_id}.yaml"
        if candidate.is_file():
            # Path-resolve check — guard against symlink escape (audit #9 +
            # Codex P2-3). The candidate must resolve to a path still under
            # the base.
            resolved = candidate.resolve()
            base_resolved = base.resolve()
            try:
                resolved.relative_to(base_resolved)
            except ValueError:
                raise InvalidClientIdError(
                    f"resolved path {resolved} escapes base {base_resolved}"
                )
            return candidate
    raise FileNotFoundError(
        f"no client config found for {client_id!r} in {PROD_CLIENT_DIR} or {LOCAL_CLIENT_DIR}"
    )


def _build_axis(raw: dict | None) -> AxisConfig:
    raw = raw or {}
    return AxisConfig(
        label=raw.get("label", ""),
        keywords=list(raw.get("keywords") or []),
        saturation=int(raw.get("saturation") or 4),
        archetype_boosts=dict(raw.get("archetype_boosts") or {}),
    )


def _build_tier(raw: dict | None) -> TierThreshold:
    raw = raw or {}
    return TierThreshold(
        max_followers=raw.get("max_followers"),
        min_followers=raw.get("min_followers"),
        min_brokers=raw.get("min_brokers"),
        min_vibe=raw.get("min_vibe"),
    )


def _build_tier_set(raw: dict | None) -> TierThresholds:
    raw = raw or {}
    return TierThresholds(
        A=_build_tier(raw.get("A")),
        B=_build_tier(raw.get("B")),
        C=_build_tier(raw.get("C")),
    )


def load_client_config(client_id: str) -> ClientConfig:
    """Load and validate a per-client YAML config.

    Args:
        client_id: e.g. 'solstitch'. Validated against ``_CLIENT_ID_RE``.

    Returns:
        Parsed and typed :class:`ClientConfig`.

    Raises:
        InvalidClientIdError: bad client_id or symlink escape.
        FileNotFoundError: no config file for this client.
        ValueError: missing required fields in the YAML.
    """
    path = _resolve_config_path(client_id)
    with open(path, encoding="utf-8") as fh:
        raw: dict[str, Any] = yaml.safe_load(fh) or {}

    # Required fields
    yaml_id = raw.get("client_id")
    if yaml_id != client_id:
        raise ValueError(
            f"YAML client_id {yaml_id!r} doesn't match filename stem {client_id!r}"
        )
    mode = raw.get("mode")
    if mode not in ("stealth", "public"):
        raise ValueError(f"mode must be 'stealth' or 'public' (got {mode!r})")

    # Network axes — required so semantic layout works
    axes_raw = raw.get("network_axes") or {}
    network_axes = NetworkAxes(
        x=_build_axis(axes_raw.get("x")),
        y=_build_axis(axes_raw.get("y")),
    )
    if not network_axes.x.keywords or not network_axes.y.keywords:
        raise ValueError(
            f"network_axes.{{x,y}}.keywords must each be non-empty in {path}"
        )

    # Audiences (optional but typical)
    audiences_raw = raw.get("audiences") or []
    audiences = [
        AudienceConfig(
            handle=str(a["handle"]).lstrip("@").lower().strip(),
            label=str(a.get("label") or a["handle"]),
            curator_weight=float(a.get("curator_weight") or 1.0),
        )
        for a in audiences_raw
        if a and a.get("handle")
    ]

    # Tier thresholds — both modes parsed even if only one is used
    tier_raw = raw.get("tier_thresholds") or {}
    tier_thresholds = {
        "stealth": _build_tier_set(tier_raw.get("stealth")),
        "public": _build_tier_set(tier_raw.get("public")),
    }

    return ClientConfig(
        client_id=client_id,
        display_name=str(raw.get("display_name") or client_id),
        mode=mode,
        debut_date=raw.get("debut_date"),
        sector_focus=list(raw.get("sector_focus") or []),
        themes=list(raw.get("themes") or []),
        audiences=audiences,
        manual_pins=[str(p).lstrip("@").lower().strip() for p in raw.get("manual_pins") or []],
        org_denylist_extras=[str(p).lstrip("@").lower().strip() for p in raw.get("org_denylist_extras") or []],
        person_allowlist_extras=[str(p).lstrip("@").lower().strip() for p in raw.get("person_allowlist_extras") or []],
        celebrity_denylist_extras=[str(p).lstrip("@").lower().strip() for p in raw.get("celebrity_denylist_extras") or []],
        network_axes=network_axes,
        tier_thresholds=tier_thresholds,
        raw=raw,
    )


# ---------------------------------------------------------------------------
# Output paths
# ---------------------------------------------------------------------------

def outreach_output_dir(client_id: str) -> Path:
    """Return the directory where outreach artifacts go for this client.

    * Production: ``/opt/sable/outreach/<client_id>/``
    * Local dev: ``$SABLE_OUTREACH_DIR/<client_id>/`` if set, else
      ``~/Downloads/`` (no client subdir, for backwards-compat with current
      operator workflow).
    """
    assert_client_id(client_id)
    if PROD_CLIENT_DIR.exists():
        return Path(f"/opt/sable/outreach/{client_id}")
    override = os.environ.get("SABLE_OUTREACH_DIR")
    if override:
        return Path(override) / client_id
    return Path.home() / "Downloads"
