"""Tests for sable_kol.persona_priming — operator-persona priming canonical source."""
from __future__ import annotations

import json
from typing import get_args

from click.testing import CliRunner

from sable_kol.cli import cli
from sable_kol.persona_priming import (
    PERSONAS,
    PersonaSlug,
    is_placeholder,
    manifest,
    priming_for,
)


def test_persona_table_matches_literal():
    """PERSONAS keys must equal the PersonaSlug Literal — drift is a developer error."""
    assert set(PERSONAS.keys()) == set(get_args(PersonaSlug))


def test_non_placeholder_personas_have_full_priming():
    """sieggy / sparta / arf must have non-empty voice_register, opening_style, avoid."""
    real_slugs = [s for s, p in PERSONAS.items() if not p.placeholder]
    assert sorted(real_slugs) == ["arf", "sieggy", "sparta"]
    for slug in real_slugs:
        p = PERSONAS[slug]
        assert p.voice_register.strip(), f"{slug} voice_register empty"
        assert p.opening_style.strip(), f"{slug} opening_style empty"
        assert p.avoid.strip(), f"{slug} avoid empty"
        assert "<placeholder" not in p.voice_register
        assert "<placeholder" not in p.opening_style
        assert "<placeholder" not in p.avoid


def test_ben_is_placeholder():
    """Ben drafts must be 409-blocked until the operator supplies real priming."""
    assert PERSONAS["ben"].placeholder is True
    assert is_placeholder("ben") is True
    # Real personas must not be flagged placeholder.
    for real in ("sieggy", "sparta", "arf"):
        assert is_placeholder(real) is False


def test_persona_manifest_cli_emits_expected_shape():
    """`sable-kol persona-manifest --json` is the lockstep contract for SableWeb."""
    runner = CliRunner()
    result = runner.invoke(cli, ["persona-manifest", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload == manifest()
    assert sorted(payload["slugs"]) == ["arf", "ben", "sieggy", "sparta"]
    assert payload["placeholder_slugs"] == ["ben"]


def test_priming_for_returns_immutable_record():
    """priming_for() returns the frozen dataclass instance — caller can't mutate."""
    p = priming_for("sieggy")
    import dataclasses
    assert dataclasses.is_dataclass(p)
    # Frozen dataclass — assignment raises FrozenInstanceError.
    import pytest
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.voice_register = "evil mutation"  # type: ignore[misc]
