"""Tests for sable_kol.persona_priming — expanded operator profiles (KO-3 v2)."""
from __future__ import annotations

import dataclasses
import json
from typing import get_args

import pytest
from click.testing import CliRunner

from sable_kol.cli import cli
from sable_kol.persona_priming import (
    PERSONAS,
    PersonaSlug,
    is_placeholder,
    manifest,
    operator_profile_block,
    priming_for,
)


def test_persona_table_matches_literal():
    """PERSONAS keys must equal PersonaSlug Literal — drift is a developer error."""
    assert set(PERSONAS.keys()) == set(get_args(PersonaSlug))


def test_real_personas_are_arf_and_sparta():
    """Sieggy was removed in KO-3 v2 (he doesn't run outreach)."""
    real_slugs = sorted(s for s, p in PERSONAS.items() if not p.placeholder)
    assert real_slugs == ["arf", "sparta"]


def test_sieggy_persona_removed():
    """Sieggy must not be in the literal nor the dict."""
    assert "sieggy" not in get_args(PersonaSlug)
    assert "sieggy" not in PERSONAS


def test_ben_is_placeholder():
    assert PERSONAS["ben"].placeholder is True
    assert is_placeholder("ben") is True
    for real in ("arf", "sparta"):
        assert is_placeholder(real) is False


def test_real_personas_have_minimum_priming():
    """sieggy / sparta have load-bearing fields populated.

    Empty profiles would cause Grok to fabricate commonality from thin
    air. We require at minimum: non-empty bio, ≥1 theme, ≥1 community,
    non-empty voice_signature.
    """
    for slug in ("arf", "sparta"):
        p = PERSONAS[slug]
        assert p.bio.strip(), f"{slug} bio empty"
        assert "<placeholder" not in p.bio, f"{slug} bio still placeholder"
        assert p.themes, f"{slug} themes empty"
        assert p.communities, f"{slug} communities empty"
        assert p.voice_signature.strip(), f"{slug} voice_signature empty"


def test_persona_manifest_cli_emits_expected_shape():
    runner = CliRunner()
    result = runner.invoke(cli, ["persona-manifest", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload == manifest()
    assert sorted(payload["slugs"]) == ["arf", "ben", "sparta"]
    assert payload["placeholder_slugs"] == ["ben"]


def test_priming_for_returns_immutable_record():
    p = priming_for("arf")
    assert dataclasses.is_dataclass(p)
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.location = "evil mutation"  # type: ignore[misc]


def test_operator_profile_block_renders_named_sections():
    """The prompt-rendered profile carries the load-bearing sections so
    Grok can compute commonality. Format is Markdown-ish bullets."""
    block = operator_profile_block("arf")
    assert "OPERATOR PROFILE" in block
    # Header carries display_name + twitter_handle so Grok knows the
    # operator's actual on-platform identity for mutual lookups.
    assert "Arf" in block
    assert "@CahitArf11" in block
    assert "themes:" in block
    assert "communities:" in block
    assert "voice_signature:" in block


def test_operator_profile_block_omits_empty_fields():
    """Fields the operator hasn't provided are skipped, not rendered as 'null'."""
    p = priming_for("arf")
    if not p.notable_mutuals:
        block = operator_profile_block("arf")
        assert "notable_mutuals:" not in block
