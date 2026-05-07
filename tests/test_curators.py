"""Tests for curator-weighted list_vote_score."""
from __future__ import annotations

import pytest

from sable_kol import curators as curators_mod
from sable_kol import enrich as enrich_mod
from sable_kol.db import Candidate


def _stub_candidate(**kw):
    base = dict(
        candidate_id=1, twitter_id=None, handle_normalized="x", is_unresolved=0,
        handle_history=[], display_name=None, bio_snapshot=None,
        followers_snapshot=None, discovery_sources=[], first_seen_at=None,
        last_seen_at=None, archetype_tags=[], sector_tags=[],
        sable_relationship={"communities": [], "operators": []},
        enrichment_tier="none", last_enriched_at=None, status="active",
        manual_notes=None, kol_strength_score=None, verified=0,
        account_created_at=None,
    )
    base.update(kw)
    return Candidate(**base)


@pytest.fixture(autouse=True)
def reset_curators_cache(monkeypatch, tmp_path):
    """Each test gets a fresh ~/.sable/ pointed at tmp_path. Cache is cleared
    between tests so a yaml written in one test doesn't leak to another."""
    monkeypatch.setenv("SABLE_HOME", str(tmp_path))
    curators_mod.reset_cache()
    yield
    curators_mod.reset_cache()


def _write_curators_yaml(tmp_path, contents):
    p = tmp_path / "kol_list_curators.yaml"
    p.write_text(contents)


# ---------------------------------------------------------------------------
# parse_list_curator
# ---------------------------------------------------------------------------

def test_parse_three_part_label():
    assert curators_mod.parse_list_curator("list:cobie:1234") == "cobie"


def test_parse_two_part_label():
    assert curators_mod.parse_list_curator("list:1234") == "1234"


def test_parse_non_list_source_returns_none():
    assert curators_mod.parse_list_curator("cahit_list") is None
    assert curators_mod.parse_list_curator("org:tig") is None


# ---------------------------------------------------------------------------
# weight_for_list_source
# ---------------------------------------------------------------------------

def test_weight_defaults_to_one_when_no_yaml(tmp_path):
    assert curators_mod.weight_for_list_source("list:cobie:1") == 1.0
    assert curators_mod.weight_for_list_source("list:unknowncurator:99") == 1.0


def test_weight_uses_yaml_when_curator_listed(tmp_path):
    _write_curators_yaml(tmp_path, "curators:\n  coinlaunch_space: 2.0\n  default: 1.0\n")
    curators_mod.reset_cache()
    assert curators_mod.weight_for_list_source("list:coinlaunch_space:influencers") == 2.0
    assert curators_mod.weight_for_list_source("list:cobie:1") == 1.0  # default


def test_weight_default_key_overrides_implicit_default(tmp_path):
    _write_curators_yaml(tmp_path, "curators:\n  default: 0.3\n")
    curators_mod.reset_cache()
    assert curators_mod.weight_for_list_source("list:anyone:1") == 0.3


def test_weight_zero_for_non_list_source():
    assert curators_mod.weight_for_list_source("cahit_list") == 0.0
    assert curators_mod.weight_for_list_source("org:tig") == 0.0


def test_weight_handles_malformed_yaml(tmp_path):
    """Bad yaml shouldn't crash — we degrade to all-default."""
    _write_curators_yaml(tmp_path, "this is not yaml: [but has unclosed bracket")
    curators_mod.reset_cache()
    assert curators_mod.weight_for_list_source("list:cobie:1") == 1.0


# ---------------------------------------------------------------------------
# compute_kol_strength integration
# ---------------------------------------------------------------------------

def test_strength_scales_with_weighted_votes(tmp_path):
    _write_curators_yaml(tmp_path, "curators:\n  coinlaunch_space: 2.0\n  default: 1.0\n")
    curators_mod.reset_cache()

    c_one_default = _stub_candidate(discovery_sources=["list:cobie:1"])
    c_one_elite = _stub_candidate(discovery_sources=["list:coinlaunch_space:influencers"])
    # Elite curator's single vote outscores a regular curator's single vote.
    assert enrich_mod.compute_kol_strength(c_one_elite) > enrich_mod.compute_kol_strength(c_one_default)


def test_strength_caps_at_five_weighted_votes(tmp_path):
    _write_curators_yaml(tmp_path, "curators:\n  coinlaunch_space: 2.0\n  default: 1.0\n")
    curators_mod.reset_cache()

    # 3 elite votes = 6 weighted, caps at 5.
    c_three_elite = _stub_candidate(discovery_sources=[
        "list:coinlaunch_space:a", "list:coinlaunch_space:b", "list:coinlaunch_space:c",
    ])
    # 5 default votes = 5 weighted, hits the cap exactly.
    c_five_default = _stub_candidate(discovery_sources=[
        "list:c1:1", "list:c2:2", "list:c3:3", "list:c4:4", "list:c5:5",
    ])
    assert enrich_mod.compute_kol_strength(c_three_elite) == enrich_mod.compute_kol_strength(c_five_default)


def test_strength_stacks_across_lists(tmp_path):
    _write_curators_yaml(tmp_path, "curators:\n  default: 1.0\n")
    curators_mod.reset_cache()

    c_one = _stub_candidate(discovery_sources=["list:c1:1"])
    c_three = _stub_candidate(discovery_sources=["list:c1:1", "list:c2:2", "list:c3:3"])
    assert enrich_mod.compute_kol_strength(c_three) > enrich_mod.compute_kol_strength(c_one)


def test_strength_ignores_non_list_sources(tmp_path):
    """org:* and bare cahit_list don't add to kol_strength."""
    _write_curators_yaml(tmp_path, "curators:\n  default: 1.0\n")
    curators_mod.reset_cache()
    c = _stub_candidate(discovery_sources=["org:tig", "cahit_list"])
    assert enrich_mod.compute_kol_strength(c) == 0.0
