"""Curator trust weights for the multi-list KOL strength signal.

A ``discovery_source`` of the form ``list:<curator>:<list_id>`` (or the simpler
``list:<list_id>``) contributes one weighted vote to ``list_vote_score`` in
``compute_kol_strength``. The weight comes from
``~/.sable/kol_list_curators.yaml``:

    curators:
      coinlaunch_space: 2.0      # editorial directory — vetted, counts more
      delphi_digital:   1.5
      cobie:            1.2
      default:          1.0      # fallback for unlisted curators

Curators not listed (or label-less ``list:<id>:<id>`` placeholder labels)
fall back to ``default``. If the file is absent, every list:* source counts
1.0.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml


DEFAULT_CURATOR_WEIGHT = 1.0


@lru_cache(maxsize=1)
def _load_yaml() -> dict[str, float]:
    home = Path(os.environ.get("SABLE_HOME") or (Path.home() / ".sable"))
    p = home / "kol_list_curators.yaml"
    if not p.exists():
        return {}
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        raw = data.get("curators") or {}
        return {str(k): float(v) for k, v in raw.items()}
    except Exception:
        return {}


def _default_weight(weights: dict[str, float]) -> float:
    return float(weights.get("default", DEFAULT_CURATOR_WEIGHT))


def parse_list_curator(source: str) -> str | None:
    """Return the curator slug from a ``list:`` discovery_source label.

    Accepts ``list:<curator>:<list_id>`` (3-part) or ``list:<list_id>`` (2-part).
    For 2-part labels, the slug IS the list_id (so the yaml can target it
    directly if you want to weight by raw list_id).
    """
    if not source.startswith("list:"):
        return None
    parts = source.split(":", 2)
    if len(parts) < 2:
        return None
    return parts[1]


def weight_for_list_source(source: str) -> float:
    """Look up the trust weight for a single ``list:`` source label.

    Non-list sources return 0.0 (they don't contribute to list_vote_score).
    """
    curator = parse_list_curator(source)
    if curator is None:
        return 0.0
    weights = _load_yaml()
    if curator in weights:
        return weights[curator]
    return _default_weight(weights)


def reset_cache() -> None:
    """Drop the cached yaml load. Tests use this between fixtures."""
    _load_yaml.cache_clear()
