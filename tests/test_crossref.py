"""Tests for crossref — uses an in-memory sable.db with seeded entities."""
from __future__ import annotations

from contextlib import contextmanager

from sable_kol import crossref as crossref_mod
from sable_kol.db import get_candidate_by_handle, upsert_candidate


def _patch_open_db(monkeypatch, conn):
    @contextmanager
    def _fake():
        yield conn

    monkeypatch.setattr(crossref_mod, "open_db", _fake)


def _seed_org(conn, org_id: str, twitter_handle: str | None = None):
    conn.execute(
        "INSERT INTO orgs (org_id, display_name, twitter_handle) VALUES (:id, :name, :h)",
        {"id": org_id, "name": org_id.upper(), "h": twitter_handle},
    )
    conn.commit()


def _seed_entity(
    conn,
    *,
    entity_id: str,
    org_id: str,
    display_name: str,
    twitter_handle: str | None = None,
    status: str = "candidate",
    tags: list[str] | None = None,
):
    conn.execute(
        "INSERT INTO entities (entity_id, org_id, display_name, status) "
        "VALUES (:eid, :oid, :name, :status)",
        {"eid": entity_id, "oid": org_id, "name": display_name, "status": status},
    )
    if twitter_handle:
        conn.execute(
            "INSERT INTO entity_handles (entity_id, platform, handle) "
            "VALUES (:eid, 'twitter', :h)",
            {"eid": entity_id, "h": twitter_handle},
        )
    for t in tags or []:
        conn.execute(
            "INSERT INTO entity_tags (entity_id, tag, is_current) VALUES (:eid, :tag, 1)",
            {"eid": entity_id, "tag": t},
        )
    conn.commit()


def test_match_pass_writes_relationship(db_conn, monkeypatch):
    _seed_org(db_conn, "tig")
    _seed_entity(
        db_conn,
        entity_id="ent_alice_tig",
        org_id="tig",
        display_name="Alice",
        twitter_handle="alice",
        status="confirmed",
        tags=["voice", "tracked"],
    )
    upsert_candidate(db_conn, handle="alice", discovery_source="cahit_list")
    _patch_open_db(monkeypatch, db_conn)

    summary = crossref_mod.run_crossref()
    assert summary.matched == 1

    cand = get_candidate_by_handle(db_conn, "alice")
    assert len(cand.sable_relationship["communities"]) == 1
    community = cand.sable_relationship["communities"][0]
    assert community["org_id"] == "tig"
    assert "voice" in community["tags"]
    assert community["entity_status"] == "confirmed"
    assert "org:tig" in cand.discovery_sources


def test_no_match_leaves_relationship_empty(db_conn, monkeypatch):
    upsert_candidate(db_conn, handle="orphan", discovery_source="cahit_list")
    _patch_open_db(monkeypatch, db_conn)
    summary = crossref_mod.run_crossref()
    assert summary.matched == 0
    cand = get_candidate_by_handle(db_conn, "orphan")
    assert cand.sable_relationship == {"communities": [], "operators": []}


def test_tier2_adds_voice_entity_not_in_bank(db_conn, monkeypatch):
    _seed_org(db_conn, "tig")
    _seed_entity(
        db_conn,
        entity_id="ent_voiceguy",
        org_id="tig",
        display_name="Voice Guy",
        twitter_handle="voiceguy",
        tags=["voice"],
    )
    _patch_open_db(monkeypatch, db_conn)

    summary = crossref_mod.run_crossref()
    assert summary.tier2_added == 1

    cand = get_candidate_by_handle(db_conn, "voiceguy")
    assert cand is not None
    assert "sable_db_voice" in cand.discovery_sources


def test_tier2_adds_confirmed_entity_without_voice_tag(db_conn, monkeypatch):
    _seed_org(db_conn, "multisynq")
    _seed_entity(
        db_conn,
        entity_id="ent_confirmed",
        org_id="multisynq",
        display_name="Confirmed One",
        twitter_handle="confirmedone",
        status="confirmed",
    )
    _patch_open_db(monkeypatch, db_conn)

    summary = crossref_mod.run_crossref()
    assert summary.tier2_added == 1
    cand = get_candidate_by_handle(db_conn, "confirmedone")
    assert "sable_db_confirmed" in cand.discovery_sources


def test_tier2_does_not_duplicate_existing(db_conn, monkeypatch):
    _seed_org(db_conn, "tig")
    _seed_entity(
        db_conn,
        entity_id="ent_alice",
        org_id="tig",
        display_name="Alice",
        twitter_handle="alice",
        tags=["voice"],
    )
    upsert_candidate(db_conn, handle="alice", discovery_source="cahit_list")
    _patch_open_db(monkeypatch, db_conn)

    summary = crossref_mod.run_crossref()
    assert summary.tier2_added == 0  # alice already in bank
    cand = get_candidate_by_handle(db_conn, "alice")
    # Both sources are now present.
    assert "cahit_list" in cand.discovery_sources
    assert "sable_db_voice" in cand.discovery_sources


def test_skips_archived_entities(db_conn, monkeypatch):
    """Entities with status='archived' and no voice tag must not feed Tier-2."""
    _seed_org(db_conn, "psy")
    _seed_entity(
        db_conn,
        entity_id="ent_archived",
        org_id="psy",
        display_name="Archived",
        twitter_handle="archived",
        status="archived",
    )
    _patch_open_db(monkeypatch, db_conn)
    summary = crossref_mod.run_crossref()
    assert summary.tier2_added == 0
    assert get_candidate_by_handle(db_conn, "archived") is None


def test_handle_match_is_case_insensitive(db_conn, monkeypatch):
    _seed_org(db_conn, "tig")
    _seed_entity(
        db_conn,
        entity_id="ent_alice",
        org_id="tig",
        display_name="Alice",
        twitter_handle="Alice",  # mixed case in entity_handles
        status="confirmed",
    )
    upsert_candidate(db_conn, handle="alice", discovery_source="cahit_list")  # lowercase
    _patch_open_db(monkeypatch, db_conn)
    summary = crossref_mod.run_crossref()
    assert summary.matched == 1
