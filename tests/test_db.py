"""Tests for sable_kol.db helpers."""
from __future__ import annotations

import json

from sable_kol.db import (
    bank_stats,
    get_candidate_by_handle,
    get_external_profile,
    list_candidates,
    list_open_conflicts,
    list_unclassified,
    mark_external_profile_used,
    normalize_handle,
    update_classification,
    update_conflict_state,
    update_relationship,
    upsert_candidate,
    upsert_external_profile,
)


# ---------------------------------------------------------------------------
# normalize_handle
# ---------------------------------------------------------------------------

def test_normalize_handle_lowercases_and_strips_at():
    assert normalize_handle("@Alice") == "alice"
    assert normalize_handle("  Bob  ") == "bob"
    assert normalize_handle("@CarolEth") == "caroleth"


# ---------------------------------------------------------------------------
# upsert_candidate
# ---------------------------------------------------------------------------

def test_upsert_inserts_new_live_row(db_conn):
    res = upsert_candidate(
        db_conn,
        handle="alice",
        display_name="Alice",
        bio_snapshot="DeFi researcher",
        followers_snapshot=12000,
        discovery_source="cahit_list",
    )
    assert res.inserted is True
    assert res.updated is False
    assert res.conflicted is False
    cand = get_candidate_by_handle(db_conn, "alice")
    assert cand is not None
    assert cand.is_unresolved == 0
    assert cand.discovery_sources == ["cahit_list"]
    assert cand.status == "active"
    # JSON defaults populated
    assert cand.archetype_tags == []
    assert cand.sable_relationship == {"communities": [], "operators": []}


def test_upsert_appends_discovery_source(db_conn):
    upsert_candidate(db_conn, handle="bob", discovery_source="cahit_list")
    res = upsert_candidate(db_conn, handle="bob", discovery_source="org:tig")
    assert res.updated is True
    assert res.inserted is False
    cand = get_candidate_by_handle(db_conn, "bob")
    assert cand.discovery_sources == ["cahit_list", "org:tig"]


def test_upsert_does_not_duplicate_same_source(db_conn):
    upsert_candidate(db_conn, handle="carol", discovery_source="cahit_list")
    upsert_candidate(db_conn, handle="carol", discovery_source="cahit_list")
    cand = get_candidate_by_handle(db_conn, "carol")
    assert cand.discovery_sources == ["cahit_list"]


def test_upsert_fills_blank_fields_on_update(db_conn):
    upsert_candidate(db_conn, handle="dave", discovery_source="cahit_list")
    upsert_candidate(
        db_conn,
        handle="dave",
        discovery_source="manual",
        display_name="Dave",
        bio_snapshot="Solana dev",
        followers_snapshot=5000,
    )
    cand = get_candidate_by_handle(db_conn, "dave")
    assert cand.display_name == "Dave"
    assert cand.bio_snapshot == "Solana dev"
    assert cand.followers_snapshot == 5000


def test_upsert_with_matching_twitter_id_updates_same_row(db_conn):
    upsert_candidate(db_conn, handle="eve", twitter_id="111", discovery_source="a")
    res = upsert_candidate(db_conn, handle="eve", twitter_id="111", discovery_source="b")
    assert res.conflicted is False
    assert res.updated is True


def test_upsert_with_conflicting_twitter_id_creates_unresolved(db_conn):
    """Live row has twitter_id=111. Incoming twitter_id=222 with same handle
    means recycled-handle case — new row is_unresolved=1, conflict logged."""
    upsert_candidate(db_conn, handle="frank", twitter_id="111", discovery_source="a")
    res = upsert_candidate(db_conn, handle="frank", twitter_id="222", discovery_source="b")
    assert res.conflicted is True
    assert res.conflict_id is not None
    # Live row still the original
    live = get_candidate_by_handle(db_conn, "frank")
    assert live.twitter_id == "111"
    # Conflict registered
    conflicts = list_open_conflicts(db_conn)
    assert len(conflicts) == 1
    assert conflicts[0]["resolved_twitter_id"] == "222"
    assert conflicts[0]["resolution_state"] == "open"


# ---------------------------------------------------------------------------
# list_candidates / list_unclassified
# ---------------------------------------------------------------------------

def test_list_candidates_filters_status(db_conn):
    upsert_candidate(db_conn, handle="active1", discovery_source="x")
    upsert_candidate(db_conn, handle="dropped1", discovery_source="x")
    update_classification(
        db_conn,
        candidate_id=get_candidate_by_handle(db_conn, "dropped1").candidate_id,
        archetype_tags=[],
        sector_tags=[],
        status="dropped",
    )
    actives = list_candidates(db_conn, status="active")
    assert {c.handle_normalized for c in actives} == {"active1"}


def test_list_unclassified_skips_classified(db_conn):
    upsert_candidate(db_conn, handle="raw", discovery_source="x")
    upsert_candidate(db_conn, handle="done", discovery_source="x")
    update_classification(
        db_conn,
        candidate_id=get_candidate_by_handle(db_conn, "done").candidate_id,
        archetype_tags=["thought_leader"],
        sector_tags=["defi"],
        status="active",
    )
    unclassified = list_unclassified(db_conn)
    assert {c.handle_normalized for c in unclassified} == {"raw"}


# ---------------------------------------------------------------------------
# update_classification + update_relationship
# ---------------------------------------------------------------------------

def test_update_classification_writes_tags_and_status(db_conn):
    upsert_candidate(db_conn, handle="zara", discovery_source="x")
    cand = get_candidate_by_handle(db_conn, "zara")
    update_classification(
        db_conn,
        candidate_id=cand.candidate_id,
        archetype_tags=["thought_leader", "researcher"],
        sector_tags=["defi", "sol"],
        status="active",
    )
    fresh = get_candidate_by_handle(db_conn, "zara")
    assert fresh.archetype_tags == ["thought_leader", "researcher"]
    assert fresh.sector_tags == ["defi", "sol"]


def test_update_relationship_writes_strict_schema(db_conn):
    upsert_candidate(db_conn, handle="quinn", discovery_source="cahit_list")
    cand = get_candidate_by_handle(db_conn, "quinn")
    rel = {
        "communities": [{"org_id": "tig", "last_seen": "2026-04-20", "tags": ["voice"]}],
        "operators": [{"name": "alice", "relation": "follows"}],
    }
    update_relationship(
        db_conn,
        candidate_id=cand.candidate_id,
        relationship=rel,
        extra_discovery_source="org:tig",
    )
    fresh = get_candidate_by_handle(db_conn, "quinn")
    assert fresh.sable_relationship == rel
    assert "org:tig" in fresh.discovery_sources


# ---------------------------------------------------------------------------
# project_profiles_external
# ---------------------------------------------------------------------------

def test_external_profile_upsert_and_fetch(db_conn):
    upsert_external_profile(
        db_conn,
        handle="newproject",
        sector_tags=["defi", "rwa"],
        themes=["yield", "treasury"],
        profile_blob="A real-world-asset yield protocol",
        enrichment_source="manual_only",
    )
    prof = get_external_profile(db_conn, "newproject")
    assert prof is not None
    assert prof.sector_tags == ["defi", "rwa"]
    assert prof.themes == ["yield", "treasury"]
    assert prof.enrichment_source == "manual_only"
    assert prof.last_enriched_at is None  # manual_only never sets enriched_at


def test_external_profile_paid_basic_sets_enriched_at(db_conn):
    upsert_external_profile(
        db_conn,
        handle="paid_project",
        sector_tags=["gaming"],
        themes=[],
        profile_blob="bio from SocialData",
        enrichment_source="paid_basic",
        twitter_id="555",
        mark_enriched_now=True,
    )
    prof = get_external_profile(db_conn, "paid_project")
    assert prof.enrichment_source == "paid_basic"
    assert prof.last_enriched_at is not None
    assert prof.twitter_id == "555"


def test_mark_external_profile_used_bumps_timestamp(db_conn):
    upsert_external_profile(
        db_conn,
        handle="touched",
        sector_tags=[],
        themes=[],
        profile_blob=None,
        enrichment_source="manual_only",
    )
    before = get_external_profile(db_conn, "touched")
    mark_external_profile_used(db_conn, "touched")
    after = get_external_profile(db_conn, "touched")
    assert after.last_used_at >= before.last_used_at


# ---------------------------------------------------------------------------
# Conflicts
# ---------------------------------------------------------------------------

def test_update_conflict_state_marks_resolved(db_conn):
    upsert_candidate(db_conn, handle="hank", twitter_id="111", discovery_source="a")
    res = upsert_candidate(db_conn, handle="hank", twitter_id="222", discovery_source="b")
    assert res.conflicted is True
    update_conflict_state(db_conn, conflict_id=res.conflict_id, state="discarded", notes="dup")
    open_after = list_open_conflicts(db_conn)
    assert open_after == []


# ---------------------------------------------------------------------------
# bank_stats
# ---------------------------------------------------------------------------

def test_bank_stats_counts(db_conn):
    upsert_candidate(db_conn, handle="a", discovery_source="cahit_list")
    upsert_candidate(db_conn, handle="b", discovery_source="cahit_list")
    update_classification(
        db_conn,
        candidate_id=get_candidate_by_handle(db_conn, "a").candidate_id,
        archetype_tags=["thought_leader"],
        sector_tags=["defi"],
        status="active",
    )
    stats = bank_stats(db_conn)
    assert stats["total_live"] == 2
    assert stats["classified"] == 1
    assert stats["unclassified"] == 1
    assert stats["open_conflicts"] == 0
