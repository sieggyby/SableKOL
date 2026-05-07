"""Tests for sable_kol.client_config — YAML loader + validation."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from sable_kol import client_config as cc


@pytest.fixture
def tmp_config_dir(tmp_path, monkeypatch):
    """Redirect both PROD and LOCAL config dirs to a temp dir for isolation."""
    monkeypatch.setattr(cc, "PROD_CLIENT_DIR", tmp_path / "prod")
    monkeypatch.setattr(cc, "LOCAL_CLIENT_DIR", tmp_path / "local")
    (tmp_path / "local").mkdir(parents=True)
    yield tmp_path / "local"


def _good_yaml(client_id: str = "testclient") -> dict:
    return {
        "client_id": client_id,
        "display_name": "Test Client",
        "mode": "stealth",
        "debut_date": "2026-05-28",
        "sector_focus": ["fashion", "art"],
        "themes": ["fashion", "art"],
        "audiences": [
            {"handle": "doji_com", "label": "doji", "curator_weight": 2.0},
            {"handle": "@thefabricant", "label": "fab", "curator_weight": 1.8},
        ],
        "manual_pins": ["@loomdart", "toomuchlag"],
        "org_denylist_extras": [],
        "person_allowlist_extras": [],
        "celebrity_denylist_extras": [],
        "network_axes": {
            "x": {
                "label": "fashion",
                "keywords": ["fashion", "art"],
                "saturation": 4,
                "archetype_boosts": {"artist": 0.5},
            },
            "y": {
                "label": "crypto",
                "keywords": ["nft", "web3"],
                "saturation": 4,
            },
        },
        "tier_thresholds": {
            "stealth": {
                "A": {"max_followers": 15000, "min_brokers": 4, "min_vibe": 0.4},
                "B": {"max_followers": 100000, "min_vibe": 0.3},
                "C": {"max_followers": 100000, "min_vibe": 0.2, "min_brokers": 2},
            },
            "public": {
                "A": {"min_followers": 100000},
                "B": {"min_followers": 10000},
                "C": {"min_followers": 1000},
            },
        },
    }


# ---------------------------------------------------------------------------
# assert_client_id
# ---------------------------------------------------------------------------

def test_assert_client_id_accepts_valid():
    for ok in ("solstitch", "tig", "client_1", "abc-123", "a"):
        cc.assert_client_id(ok)  # no raise


def test_assert_client_id_rejects_path_traversal():
    for bad in ("../etc", "..", "/etc/passwd", "solstitch/.yaml", ".."):
        with pytest.raises(cc.InvalidClientIdError):
            cc.assert_client_id(bad)


def test_assert_client_id_rejects_uppercase_and_dots():
    for bad in ("SolStitch", "client.id", "test client", ""):
        with pytest.raises(cc.InvalidClientIdError):
            cc.assert_client_id(bad)


def test_assert_client_id_rejects_too_long():
    with pytest.raises(cc.InvalidClientIdError):
        cc.assert_client_id("a" * 33)


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def test_load_client_config_happy_path(tmp_config_dir):
    (tmp_config_dir / "testclient.yaml").write_text(yaml.dump(_good_yaml()))
    config = cc.load_client_config("testclient")
    assert config.client_id == "testclient"
    assert config.display_name == "Test Client"
    assert config.mode == "stealth"
    assert config.debut_date == "2026-05-28"


def test_load_normalizes_audience_handles(tmp_config_dir):
    """@-prefix and case stripped from audience handles + manual_pins."""
    (tmp_config_dir / "testclient.yaml").write_text(yaml.dump(_good_yaml()))
    config = cc.load_client_config("testclient")
    assert config.audiences[1].handle == "thefabricant"  # @ stripped
    assert "loomdart" in config.manual_pins  # @ stripped
    assert "toomuchlag" in config.manual_pins


def test_load_parses_network_axes(tmp_config_dir):
    (tmp_config_dir / "testclient.yaml").write_text(yaml.dump(_good_yaml()))
    config = cc.load_client_config("testclient")
    assert config.network_axes.x.label == "fashion"
    assert "fashion" in config.network_axes.x.keywords
    assert config.network_axes.x.archetype_boosts == {"artist": 0.5}
    assert config.network_axes.y.archetype_boosts == {}


def test_load_parses_tier_thresholds_both_modes(tmp_config_dir):
    (tmp_config_dir / "testclient.yaml").write_text(yaml.dump(_good_yaml()))
    config = cc.load_client_config("testclient")
    assert config.tier_thresholds["stealth"].A.max_followers == 15000
    assert config.tier_thresholds["stealth"].A.min_brokers == 4
    assert config.tier_thresholds["public"].A.min_followers == 100000


def test_load_rejects_yaml_id_mismatch(tmp_config_dir):
    """YAML client_id must match the filename stem."""
    bad = _good_yaml(client_id="someoneelse")
    (tmp_config_dir / "testclient.yaml").write_text(yaml.dump(bad))
    with pytest.raises(ValueError, match="client_id"):
        cc.load_client_config("testclient")


def test_load_rejects_invalid_mode(tmp_config_dir):
    bad = _good_yaml()
    bad["mode"] = "bogus"
    (tmp_config_dir / "testclient.yaml").write_text(yaml.dump(bad))
    with pytest.raises(ValueError, match="mode must be"):
        cc.load_client_config("testclient")


def test_load_rejects_empty_axis_keywords(tmp_config_dir):
    bad = _good_yaml()
    bad["network_axes"]["x"]["keywords"] = []
    (tmp_config_dir / "testclient.yaml").write_text(yaml.dump(bad))
    with pytest.raises(ValueError, match="keywords"):
        cc.load_client_config("testclient")


def test_load_missing_file(tmp_config_dir):
    with pytest.raises(FileNotFoundError):
        cc.load_client_config("nonexistent")


def test_load_rejects_invalid_client_id():
    with pytest.raises(cc.InvalidClientIdError):
        cc.load_client_config("../etc")


# ---------------------------------------------------------------------------
# discovered_client_ids
# ---------------------------------------------------------------------------

def test_discovered_client_ids_lists_yaml_files(tmp_config_dir):
    (tmp_config_dir / "alice.yaml").write_text(yaml.dump(_good_yaml("alice")))
    (tmp_config_dir / "bob.yaml").write_text(yaml.dump(_good_yaml("bob")))
    (tmp_config_dir / "not-yaml.txt").write_text("ignore me")
    discovered = cc.discovered_client_ids()
    assert "alice" in discovered
    assert "bob" in discovered
    assert "not-yaml" not in discovered


def test_discovered_client_ids_handles_missing_dirs(tmp_path, monkeypatch):
    """Both base dirs absent → empty set, no exception."""
    monkeypatch.setattr(cc, "PROD_CLIENT_DIR", tmp_path / "nope1")
    monkeypatch.setattr(cc, "LOCAL_CLIENT_DIR", tmp_path / "nope2")
    assert cc.discovered_client_ids() == set()
