"""Tests for profile builders."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from sable_kol.profile import (
    PAID_PROFILE_TTL_SECONDS,
    _is_stale,
    build_external_profile,
    build_org_profile,
    read_voice_blob,
)


# ---------------------------------------------------------------------------
# Voice docs
# ---------------------------------------------------------------------------

def test_read_voice_blob_concats_files(tmp_path, monkeypatch):
    monkeypatch.setenv("SABLE_HOME", str(tmp_path))
    base = tmp_path / "profiles" / "@alice"
    base.mkdir(parents=True)
    (base / "tone.md").write_text("warm and lateral")
    (base / "interests.md").write_text("yield, MEV")
    blob = read_voice_blob("alice")
    assert "warm and lateral" in blob
    assert "yield, MEV" in blob
    assert "## tone.md" in blob
    assert "## interests.md" in blob


def test_read_voice_blob_returns_none_when_dir_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("SABLE_HOME", str(tmp_path))
    assert read_voice_blob("nosuch") is None


def test_read_voice_blob_returns_none_when_dir_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("SABLE_HOME", str(tmp_path))
    (tmp_path / "profiles" / "@empty").mkdir(parents=True)
    assert read_voice_blob("empty") is None


# ---------------------------------------------------------------------------
# Path (i) — org profile
# ---------------------------------------------------------------------------

def _seed_org(conn, org_id, twitter_handle=None, config=None):
    conn.execute(
        "INSERT INTO orgs (org_id, display_name, twitter_handle, config_json) "
        "VALUES (:id, :name, :h, :cfg)",
        {
            "id": org_id,
            "name": org_id.upper(),
            "h": twitter_handle,
            "cfg": json.dumps(config or {}),
        },
    )
    conn.commit()


def _seed_entity_with_tags(conn, *, entity_id, org_id, tags):
    conn.execute(
        "INSERT INTO entities (entity_id, org_id, display_name, status) "
        "VALUES (:eid, :oid, :name, 'confirmed')",
        {"eid": entity_id, "oid": org_id, "name": entity_id},
    )
    for t in tags:
        conn.execute(
            "INSERT INTO entity_tags (entity_id, tag, is_current) VALUES (:eid, :tag, 1)",
            {"eid": entity_id, "tag": t},
        )
    conn.commit()


def test_build_org_profile_pulls_sector_stage_tags(db_conn, tmp_path, monkeypatch):
    monkeypatch.setenv("SABLE_HOME", str(tmp_path))
    _seed_org(
        db_conn,
        "tig",
        twitter_handle="tigfoundation",
        config={"sector": "DeSci", "stage": "growth", "themes": ["agi", "swarm"]},
    )
    _seed_entity_with_tags(db_conn, entity_id="ent1", org_id="tig", tags=["voice", "tracked"])
    _seed_entity_with_tags(db_conn, entity_id="ent2", org_id="tig", tags=["voice"])

    p = build_org_profile(db_conn, "tig")
    assert p.source == "org"
    assert p.org_id == "tig"
    assert p.sector == "DeSci"
    assert p.stage == "growth"
    assert p.themes == ["agi", "swarm"]
    # Top tags ordered by frequency desc — voice should lead (2 vs 1).
    assert p.top_tags[0] == "voice"
    assert "tracked" in p.top_tags
    assert p.handle == "tigfoundation"
    assert p.voice_blob is None  # no voice doc dir


def test_build_org_profile_reads_voice_blob_when_present(db_conn, tmp_path, monkeypatch):
    monkeypatch.setenv("SABLE_HOME", str(tmp_path))
    _seed_org(
        db_conn,
        "tig",
        twitter_handle="tigfoundation",
        config={"sector": "DeSci"},
    )
    base = tmp_path / "profiles" / "@tigfoundation"
    base.mkdir(parents=True)
    (base / "tone.md").write_text("playful, technical, never preachy")
    p = build_org_profile(db_conn, "tig")
    assert p.voice_blob is not None
    assert "playful, technical" in p.voice_blob


def test_build_org_profile_unknown_org_raises(db_conn):
    with pytest.raises(LookupError):
        build_org_profile(db_conn, "nope")


# ---------------------------------------------------------------------------
# Path (ii) — external profile
# ---------------------------------------------------------------------------

def test_external_profile_manual_only_creates_row(db_conn):
    p = build_external_profile(
        db_conn,
        handle="newproject",
        sector="DeFi",
        themes=["yield", "rwa"],
        paid_enrich=False,
    )
    assert p.source == "external_manual"
    assert p.sector == "DeFi"
    assert p.themes == ["yield", "rwa"]


def test_external_profile_paid_enrich_calls_socialdata(db_conn):
    calls = []

    def fake_fetcher(handle):
        calls.append(handle)
        return {
            "id_str": "1234567",
            "name": "New Project",
            "description": "yield aggregator",
            "followers_count": 50000,
            "verified": True,
        }

    p = build_external_profile(
        db_conn,
        handle="newproject",
        sector="DeFi",
        themes=["yield"],
        paid_enrich=True,
        socialdata_fetcher=fake_fetcher,
    )
    assert p.source == "external_paid_basic"
    assert calls == ["newproject"]
    # Cost event was logged.
    ev = db_conn.execute(
        "SELECT * FROM cost_events WHERE call_type = 'sablekol.socialdata_user_profile'"
    ).fetchone()
    assert ev is not None
    assert ev["call_status"] == "success"
    assert abs(ev["cost_usd"] - 0.002) < 1e-9
    # voice_blob now contains the bio.
    assert p.voice_blob is not None
    assert "yield aggregator" in p.voice_blob


def test_external_profile_paid_enrich_uses_cache_within_ttl(db_conn):
    """Second call within TTL must NOT fetch."""
    calls = []

    def fake_fetcher(handle):
        calls.append(handle)
        return {"description": "first call only", "followers_count": 1000}

    build_external_profile(
        db_conn,
        handle="newproject",
        sector="DeFi",
        paid_enrich=True,
        socialdata_fetcher=fake_fetcher,
    )
    assert len(calls) == 1
    build_external_profile(
        db_conn,
        handle="newproject",
        sector="DeFi",
        paid_enrich=True,
        socialdata_fetcher=fake_fetcher,
    )
    assert len(calls) == 1  # cache hit, no second call


def test_external_profile_refresh_paid_forces_fetch(db_conn):
    calls = []

    def fake_fetcher(handle):
        calls.append(handle)
        return {"description": "fresh", "followers_count": 1234}

    build_external_profile(
        db_conn, handle="x", sector="DeFi",
        paid_enrich=True, socialdata_fetcher=fake_fetcher,
    )
    build_external_profile(
        db_conn, handle="x", sector="DeFi",
        paid_enrich=True, refresh_paid=True, socialdata_fetcher=fake_fetcher,
    )
    assert len(calls) == 2


def test_external_profile_fetcher_error_logs_failed_cost_event(db_conn):
    def boom(_handle):
        raise RuntimeError("network down")

    with pytest.raises(RuntimeError):
        build_external_profile(
            db_conn,
            handle="bad",
            sector="DeFi",
            paid_enrich=True,
            socialdata_fetcher=boom,
        )
    ev = db_conn.execute(
        "SELECT call_status FROM cost_events "
        "WHERE call_type = 'sablekol.socialdata_user_profile'"
    ).fetchone()
    assert ev is not None
    assert ev["call_status"] == "error"


def test_is_stale_logic():
    assert _is_stale(None, 60) is True
    fresh = "2099-01-01T00:00:00+00:00"
    assert _is_stale(fresh, 60) is False  # future date — never stale
    old = "1990-01-01T00:00:00+00:00"
    assert _is_stale(old, PAID_PROFILE_TTL_SECONDS) is True
