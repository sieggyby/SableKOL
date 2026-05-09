"""Tests for sable_kol.grok_api — the xAI client.

Uses httpx.MockTransport for canned responses so no real xAI calls happen
in CI. The live ``@solstitch`` smoke test is operator-triggered via
``sable-kol preflight solstitch`` (see deploy/SIDECAR.md).
"""
from __future__ import annotations

import json

import httpx
import pytest

from sable_kol import grok_api
from sable_kol.grok_api import (
    GrokAPIError,
    GrokAuthError,
    GrokParseError,
    enrich_handle,
    suggest_comparable_projects,
)


GOOD_ENRICH = {
    "twitter_id": "12345",
    "handle": "solstitch",
    "bio": "Tokenized fashion launchpad",
    "followers": 50000,
    "verified": False,
    "is_active": True,
    "primary_archetype": "founder",
    "primary_sectors": ["fashion", "RWA"],
    "credibility_signal": "medium",
    "real_name_known": False,
    "listed_count": 100,
    "tweets_count": 5000,
    "following": 500,
    "notes": "ground-zero socials",
    "recent_themes": ["fashion", "RWA", "streetwear"],
    "audience_archetype": "fashion-leaning crypto natives",
    "axis_candidates": [
        {"x": "fashion", "y": "crypto-native", "rationale": "core split"},
    ],
}

GOOD_COMPARABLES = {
    "comparable_projects": [
        {
            "handle": "metafactory",
            "rationale": "RWA fashion + on-chain commerce",
            "shared_themes": ["fashion", "RWA"],
        },
        {
            "handle": "rtfkt",
            "rationale": "streetwear + NFT culture",
            "shared_themes": ["fashion", "streetwear"],
        },
    ],
}


def _xai_response(content: dict, status_code: int = 200) -> httpx.Response:
    """Build a fake xAI chat-completions response wrapping `content` as JSON."""
    payload = {"choices": [{"message": {"content": json.dumps(content)}}]}
    return httpx.Response(status_code, json=payload)


def _mock_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------------------
# Auth / config
# ---------------------------------------------------------------------------


def test_enrich_handle_missing_api_key(monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    with pytest.raises(GrokAuthError, match="XAI_API_KEY"):
        enrich_handle("@solstitch")


def test_enrich_handle_auth_failure(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "x-bad")
    client = _mock_client(lambda req: httpx.Response(401, text="invalid key"))
    with pytest.raises(GrokAuthError):
        enrich_handle("@solstitch", client=client)


def test_enrich_handle_403(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "x-bad")
    client = _mock_client(lambda req: httpx.Response(403, text="forbidden"))
    with pytest.raises(GrokAuthError):
        enrich_handle("@solstitch", client=client)


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_enrich_handle_happy(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "x-test")
    client = _mock_client(lambda req: _xai_response(GOOD_ENRICH))
    result = enrich_handle("@solstitch", client=client)
    assert result.handle == "solstitch"
    assert result.primary_archetype == "founder"
    assert result.followers == 50000
    assert len(result.axis_candidates) == 1
    assert result.axis_candidates[0].x == "fashion"


def test_enrich_handle_normalizes_input(monkeypatch):
    """Handle is lowercased + @-stripped before the prompt is built and the
    response's handle field is replaced with our normalized form."""
    monkeypatch.setenv("XAI_API_KEY", "x-test")

    captured = {}

    def handler(req):
        captured["body"] = json.loads(req.content)
        # Grok echoes a different casing — we should overwrite with normalized.
        bad_echo = dict(GOOD_ENRICH)
        bad_echo["handle"] = "@SolStitch"
        return _xai_response(bad_echo)

    client = _mock_client(handler)
    result = enrich_handle("@SolStitch", client=client)
    assert result.handle == "solstitch"
    assert "@solstitch" in captured["body"]["messages"][0]["content"].lower()


def test_suggest_comparable_happy(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "x-test")
    client = _mock_client(lambda req: _xai_response(GOOD_COMPARABLES))
    result = suggest_comparable_projects("solstitch", ["fashion"], client=client)
    assert len(result) == 2
    assert {c.handle for c in result} == {"metafactory", "rtfkt"}


def test_suggest_comparable_strips_self_reference(monkeypatch):
    """Grok sometimes ignores the 'do not suggest @{handle} itself' rule.
    The client filters self-refs defensively."""
    monkeypatch.setenv("XAI_API_KEY", "x-test")
    body = {
        "comparable_projects": [
            {"handle": "SolStitch", "rationale": "self", "shared_themes": []},
            {"handle": "metafactory", "rationale": "rwa", "shared_themes": []},
        ]
    }
    client = _mock_client(lambda req: _xai_response(body))
    result = suggest_comparable_projects("solstitch", [], client=client)
    assert len(result) == 1
    assert result[0].handle == "metafactory"


# ---------------------------------------------------------------------------
# Retries
# ---------------------------------------------------------------------------


def test_enrich_handle_5xx_retry_succeeds(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "x-test")
    monkeypatch.setattr(grok_api.time, "sleep", lambda _: None)
    calls = []

    def handler(req):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(503, text="temp")
        return _xai_response(GOOD_ENRICH)

    client = _mock_client(handler)
    result = enrich_handle("@solstitch", client=client)
    assert result.handle == "solstitch"
    assert len(calls) == 2


def test_enrich_handle_5xx_exhausted_raises(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "x-test")
    monkeypatch.setattr(grok_api.time, "sleep", lambda _: None)
    client = _mock_client(lambda req: httpx.Response(503, text="down"))
    with pytest.raises(GrokAPIError):
        enrich_handle("@solstitch", client=client)


def test_enrich_handle_429_backoff(monkeypatch):
    """429 retries up to 3 times then gives up."""
    monkeypatch.setenv("XAI_API_KEY", "x-test")
    sleeps = []
    monkeypatch.setattr(grok_api.time, "sleep", lambda s: sleeps.append(s))
    client = _mock_client(lambda req: httpx.Response(429, text="rate"))
    with pytest.raises(GrokAPIError):
        enrich_handle("@solstitch", client=client)
    assert len(sleeps) >= 2  # at least 2 backoff sleeps before exhaustion


# ---------------------------------------------------------------------------
# Parse failures
# ---------------------------------------------------------------------------


def test_enrich_handle_non_json_content(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "x-test")
    bad = httpx.Response(
        200,
        json={"choices": [{"message": {"content": "not json {{{{ broken"}}]},
    )
    client = _mock_client(lambda req: bad)
    with pytest.raises(GrokParseError):
        enrich_handle("@solstitch", client=client)


def test_enrich_handle_unexpected_response_shape(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "x-test")
    bad = httpx.Response(200, json={"unexpected": "shape"})
    client = _mock_client(lambda req: bad)
    with pytest.raises(GrokParseError):
        enrich_handle("@solstitch", client=client)


def test_enrich_handle_schema_violation(monkeypatch):
    """Pydantic enum violation surfaces as GrokParseError, not Pydantic's own
    error type — operators see a single error class."""
    monkeypatch.setenv("XAI_API_KEY", "x-test")
    bad = dict(GOOD_ENRICH)
    bad["primary_archetype"] = "shitposter"  # not in the wizard enum
    client = _mock_client(lambda req: _xai_response(bad))
    with pytest.raises(GrokParseError):
        enrich_handle("@solstitch", client=client)


def test_suggest_comparable_non_list(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "x-test")
    bad = {"comparable_projects": "not a list"}
    client = _mock_client(lambda req: _xai_response(bad))
    with pytest.raises(GrokParseError):
        suggest_comparable_projects("solstitch", [], client=client)


def test_suggest_comparable_item_schema_fail(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "x-test")
    bad = {"comparable_projects": [{"no_handle": "missing required field"}]}
    client = _mock_client(lambda req: _xai_response(bad))
    with pytest.raises(GrokParseError):
        suggest_comparable_projects("solstitch", [], client=client)


# ---------------------------------------------------------------------------
# Bundled response builder
# ---------------------------------------------------------------------------


def test_build_preflight_response_signal_metadata(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "x-test")
    monkeypatch.setattr(grok_api.time, "sleep", lambda _: None)
    sequence = [GOOD_ENRICH, GOOD_COMPARABLES]

    def handler(req):
        return _xai_response(sequence.pop(0))

    client = _mock_client(handler)
    result = grok_api.build_preflight_response("@solstitch", client=client)
    assert result.handle == "solstitch"
    assert len(result.comparable_projects) == 2
    assert result.signal_metadata.source == "grok_xai_live"
    assert result.signal_metadata.signal_type == "interpretive"
    assert result.signal_metadata.model == grok_api.GROK_MODEL
    assert result.signal_metadata.fetched_at_utc.endswith("Z")
