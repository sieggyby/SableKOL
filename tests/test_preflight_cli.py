"""Tests for the `sable-kol preflight` CLI subcommand."""
from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from sable_kol.cli import cli
from sable_kol.preflight_schemas import (
    ComparableProject,
    EnrichedHandle,
    PreflightResponse,
    SignalMetadata,
)


def _fake_enriched(handle: str = "solstitch") -> EnrichedHandle:
    return EnrichedHandle(
        handle=handle,
        twitter_id="12345",
        bio="Tokenized fashion launchpad",
        followers=50000,
        verified=False,
        is_active=True,
        primary_archetype="founder",
        primary_sectors=["fashion"],
        credibility_signal="medium",
        real_name_known=False,
        recent_themes=["fashion", "RWA"],
        audience_archetype="fashion-leaning crypto natives",
        axis_candidates=[],
    )


def _fake_preflight(handle: str = "solstitch") -> PreflightResponse:
    return PreflightResponse(
        **_fake_enriched(handle).model_dump(),
        comparable_projects=[
            ComparableProject(handle="metafactory", rationale="rwa", shared_themes=["RWA"]),
        ],
        signal_metadata=SignalMetadata(
            source="grok_xai_live",
            model="grok-4-latest",
            fetched_at_utc="2026-05-09T15:00:00Z",
            signal_type="interpretive",
        ),
    )


def test_preflight_default_calls_bundled(monkeypatch):
    """Default invocation calls build_preflight_response and prints the JSON."""
    captured = {}

    def fake_build(handle):
        captured["handle"] = handle
        return _fake_preflight()

    monkeypatch.setattr("sable_kol.grok_api.build_preflight_response", fake_build)

    runner = CliRunner()
    result = runner.invoke(cli, ["preflight", "@SolStitch"])
    assert result.exit_code == 0, result.output
    assert captured["handle"] == "@SolStitch"  # CLI passes through; Grok client normalizes
    payload = json.loads(result.output)
    assert payload["handle"] == "solstitch"
    assert payload["signal_metadata"]["source"] == "grok_xai_live"
    assert len(payload["comparable_projects"]) == 1


def test_preflight_enrich_only(monkeypatch):
    """--enrich-only path calls only enrich_handle, not the comparable suggester."""
    enrich_calls = []
    suggest_calls = []

    def fake_enrich(handle, **kwargs):
        enrich_calls.append(handle)
        return _fake_enriched()

    def fake_suggest(*args, **kwargs):
        suggest_calls.append(args)
        return []

    monkeypatch.setattr("sable_kol.grok_api.enrich_handle", fake_enrich)
    monkeypatch.setattr("sable_kol.grok_api.suggest_comparable_projects", fake_suggest)
    monkeypatch.setattr(
        "sable_kol.grok_api.build_preflight_response",
        lambda *a, **k: pytest.fail("build_preflight_response should not be called"),
    )

    runner = CliRunner()
    result = runner.invoke(cli, ["preflight", "--enrich-only", "solstitch"])
    assert result.exit_code == 0, result.output
    assert len(enrich_calls) == 1
    assert len(suggest_calls) == 0
    payload = json.loads(result.output)
    # Enrich-only output has no comparable_projects field at the top level.
    assert "comparable_projects" not in payload
    assert payload["primary_archetype"] == "founder"


def test_preflight_themes_override(monkeypatch):
    """--themes splits and forwards an override list to suggest_comparable_projects."""
    forwarded_themes = []

    def fake_enrich(handle, **kwargs):
        return _fake_enriched()

    def fake_suggest(handle, themes, **kwargs):
        forwarded_themes.append(themes)
        return [
            ComparableProject(handle="metafactory", rationale="rwa", shared_themes=[]),
        ]

    monkeypatch.setattr("sable_kol.grok_api.enrich_handle", fake_enrich)
    monkeypatch.setattr("sable_kol.grok_api.suggest_comparable_projects", fake_suggest)

    runner = CliRunner()
    result = runner.invoke(
        cli, ["preflight", "--themes", "fashion, rwa, streetwear", "solstitch"]
    )
    assert result.exit_code == 0, result.output
    assert forwarded_themes == [["fashion", "rwa", "streetwear"]]
    payload = json.loads(result.output)
    assert payload["themes_override"] == ["fashion", "rwa", "streetwear"]
    assert len(payload["comparable_projects"]) == 1
