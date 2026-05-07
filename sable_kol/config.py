"""Config loaders for SableKOL.

API keys resolve in this order:
  1. environment variable (ANTHROPIC_API_KEY, SOCIALDATA_API_KEY)
  2. ~/.sable/config.yaml (anthropic_api_key, socialdata_api_key) — same
     convention used by Slopper and SablePlatform.

This avoids the operator having to source the env var separately when the
key is already in their Sable config.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any


@lru_cache(maxsize=1)
def _load_sable_config() -> dict[str, Any]:
    home = Path(os.environ.get("SABLE_HOME") or (Path.home() / ".sable"))
    cfg_path = home / "config.yaml"
    if not cfg_path.exists():
        return {}
    try:
        import yaml
        return yaml.safe_load(cfg_path.read_text()) or {}
    except Exception:
        return {}


def _resolve(env_var: str, yaml_key: str) -> str | None:
    val = os.environ.get(env_var)
    if val:
        return val
    return _load_sable_config().get(yaml_key) or None


def resolve_anthropic_api_key() -> str | None:
    return _resolve("ANTHROPIC_API_KEY", "anthropic_api_key")


def resolve_socialdata_api_key() -> str | None:
    return _resolve("SOCIALDATA_API_KEY", "socialdata_api_key")
