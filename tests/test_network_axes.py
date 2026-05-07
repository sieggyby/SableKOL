"""Tests for sable_kol.network_axes — semantic axis scoring."""
from __future__ import annotations

import pytest

from sable_kol.client_config import AxisConfig, NetworkAxes
from sable_kol.network_axes import (
    CandidateLite,
    axis_score,
    axis_scores,
)


def _fashion_axis(saturation: int = 4, boosts=None) -> AxisConfig:
    return AxisConfig(
        label="fashion",
        keywords=["fashion", "art", "design", "streetwear", "couture"],
        saturation=saturation,
        archetype_boosts=boosts or {},
    )


def _crypto_axis(saturation: int = 4, boosts=None) -> AxisConfig:
    return AxisConfig(
        label="crypto-native",
        keywords=["nft", "nfts", "web3", "onchain", "defi"],
        saturation=saturation,
        archetype_boosts=boosts or {},
    )


def test_axis_score_zero_for_no_matches():
    c = CandidateLite(bio="Just a regular trader", display_name="alice",
                      sector_tags=["other"], archetype_tags=["trader"])
    assert axis_score(c, _fashion_axis()) == 0.0


def test_axis_score_partial_match():
    c = CandidateLite(bio="streetwear designer", display_name="",
                      sector_tags=[], archetype_tags=[])
    score = axis_score(c, _fashion_axis(saturation=4))
    assert 0.2 < score < 0.3


def test_axis_score_saturates_at_one():
    c = CandidateLite(
        bio="fashion art design streetwear couture stylist photographer",
        display_name="",
        sector_tags=["fashion", "art", "design"],
        archetype_tags=[],
    )
    assert axis_score(c, _fashion_axis(saturation=4)) == 1.0


def test_axis_score_uses_sector_tags():
    c = CandidateLite(bio="", display_name="", sector_tags=["fashion", "art"],
                      archetype_tags=[])
    assert axis_score(c, _fashion_axis(saturation=4)) == 0.5


def test_axis_score_archetype_boost():
    boosts = {"artist": 0.5}
    c = CandidateLite(bio="", display_name="", sector_tags=[],
                      archetype_tags=["artist"])
    assert axis_score(c, _fashion_axis(boosts=boosts)) == 0.5


def test_axis_score_archetype_boost_plus_match():
    boosts = {"artist": 0.5}
    c = CandidateLite(
        bio="fashion art designer",
        display_name="",
        sector_tags=[],
        archetype_tags=["artist"],
    )
    assert axis_score(c, _fashion_axis(saturation=4, boosts=boosts)) == 1.0


def test_axis_score_multiple_archetype_boosts():
    boosts = {"artist": 0.3, "creator": 0.3}
    c = CandidateLite(bio="", display_name="", sector_tags=[],
                      archetype_tags=["artist", "creator"])
    assert abs(axis_score(c, _fashion_axis(boosts=boosts)) - 0.6) < 1e-9


def test_axis_score_handles_none_fields():
    c = CandidateLite(bio=None, display_name=None, sector_tags=["fashion"],
                      archetype_tags=[])
    assert axis_score(c, _fashion_axis()) == 0.25


def test_axis_score_empty_keywords_returns_zero():
    empty = AxisConfig(label="empty", keywords=[], saturation=4)
    c = CandidateLite(bio="fashion art design", display_name="", sector_tags=[],
                      archetype_tags=[])
    assert axis_score(c, empty) == 0.0


def test_axis_scores_returns_x_y_dict():
    axes = NetworkAxes(x=_fashion_axis(), y=_crypto_axis())
    c = CandidateLite(
        bio="fashion designer working onchain",
        display_name="",
        sector_tags=["fashion", "nfts"],
        archetype_tags=[],
    )
    result = axis_scores(c, axes)
    assert "x" in result and "y" in result
    assert 0 < result["x"] <= 1.0
    assert 0 < result["y"] <= 1.0


def test_axis_scores_quadrants():
    """Sanity: fashion-only scores high x; crypto-only scores high y; sweet-spot scores both."""
    axes = NetworkAxes(x=_fashion_axis(), y=_crypto_axis())
    fashion_only = CandidateLite(
        bio="fashion stylist designer",
        display_name="", sector_tags=["fashion", "art", "design"],
        archetype_tags=[],
    )
    crypto_only = CandidateLite(
        bio="defi degen onchain web3",
        display_name="", sector_tags=["nfts", "defi"],
        archetype_tags=[],
    )
    sweet_spot = CandidateLite(
        bio="onchain fashion designer",
        display_name="",
        sector_tags=["fashion", "nfts"],
        archetype_tags=[],
    )
    f = axis_scores(fashion_only, axes)
    c = axis_scores(crypto_only, axes)
    s = axis_scores(sweet_spot, axes)
    assert f["x"] > f["y"]
    assert c["y"] > c["x"]
    assert s["x"] >= 0.25 and s["y"] >= 0.25
