"""Tests for sable_kol.outreach_plan — tiering, broker fields, serialization."""
from __future__ import annotations

import pytest

from sable_kol.db import Candidate
from sable_kol.follow_graph import Cluster, CoFollowMatrix
from sable_kol import outreach_plan as op


# ---------------------------------------------------------------------------
# best_of_platform_reach
# ---------------------------------------------------------------------------

def _candidate(
    handle: str,
    *,
    x_followers: int | None = None,
    presence: dict | None = None,
    archetype: list[str] | None = None,
    score: float | None = None,
) -> Candidate:
    return Candidate(
        handle_normalized=handle,
        followers_snapshot=x_followers,
        platform_presence=presence or {},
        archetype_tags=archetype or [],
        kol_strength_score=score,
        sector_tags=["fashion"],
        discovery_sources=["doji_audience"],
    )


def test_best_of_platform_picks_max_across_platforms():
    c = _candidate(
        "alice",
        x_followers=5_000,
        presence={
            "instagram": {"followers": 80_000},
            "tiktok": {"followers": 12_000},
        },
    )
    reach, plat = op.best_of_platform_reach(c)
    assert reach == 80_000
    assert plat == "instagram"


def test_best_of_platform_x_only():
    c = _candidate("bob", x_followers=15_000)
    reach, plat = op.best_of_platform_reach(c)
    assert reach == 15_000
    assert plat == "x"


def test_best_of_platform_zero_when_no_data():
    c = _candidate("ghost")
    reach, plat = op.best_of_platform_reach(c)
    assert reach == 0
    assert plat == "x"


# ---------------------------------------------------------------------------
# assign_tier
# ---------------------------------------------------------------------------

def test_assign_tier_uses_thresholds():
    assert op.assign_tier(150_000) == "A"
    assert op.assign_tier(50_000) == "B"
    assert op.assign_tier(2_000) == "C"
    assert op.assign_tier(500) == "unranked"


def test_assign_tier_manual_pin_overrides_reach():
    assert op.assign_tier(100, manual_pin=True) == "A"


# ---------------------------------------------------------------------------
# build_plan
# ---------------------------------------------------------------------------

def _matrix(rows: list[str], follows_map: dict[str, list[str]]) -> CoFollowMatrix:
    cols_seen: list[str] = []
    cols_idx: dict[str, int] = {}
    follows: list[set[int]] = []
    for r in rows:
        s: set[int] = set()
        for c in follows_map.get(r, []):
            if c not in cols_idx:
                cols_idx[c] = len(cols_seen)
                cols_seen.append(c)
            s.add(cols_idx[c])
        follows.append(s)
    return CoFollowMatrix(rows=rows, cols=cols_seen, follows_by_row=follows)


def test_build_plan_tiers_by_best_of_platform_reach(db_conn):
    candidates = [
        _candidate("megacap", presence={"instagram": {"followers": 500_000}}, score=0.9),
        _candidate("midtier", x_followers=25_000, score=0.7),
        _candidate("smol", x_followers=2_500, score=0.5),
        _candidate("dust", x_followers=300, score=0.3),
    ]
    plan = op.build_plan(db_conn, candidates=candidates)
    by_handle = {t.handle: t for t in plan}
    assert by_handle["megacap"].tier == "A"
    assert by_handle["megacap"].primary_platform == "instagram"
    assert by_handle["midtier"].tier == "B"
    assert by_handle["smol"].tier == "C"
    assert by_handle["dust"].tier == "unranked"


def test_build_plan_manual_pin_promotes_to_tier_a(db_conn):
    candidates = [_candidate("smol", x_followers=2_500)]
    plan = op.build_plan(db_conn, candidates=candidates, manual_pins={"smol"})
    assert plan[0].tier == "A"
    assert plan[0].manual_pin is True


def test_build_plan_attaches_cluster_membership(db_conn):
    # alice and bob are in the same cluster; carol is alone.
    candidates = [
        _candidate("alice", x_followers=15_000),
        _candidate("bob", x_followers=15_000),
        _candidate("carol", x_followers=15_000),
    ]
    matrix = _matrix(
        rows=["alice", "bob", "carol"],
        follows_map={
            "alice": ["target_x", "target_y"],
            "bob": ["target_x", "target_y"],
            "carol": ["unrelated"],
        },
    )
    clusters = [
        Cluster(cluster_id=0, members=["alice", "bob"]),
        Cluster(cluster_id=1, members=["carol"]),
    ]
    plan = op.build_plan(
        db_conn,
        candidates=candidates,
        co_follow_matrix=matrix,
        clusters=clusters,
    )
    by_handle = {t.handle: t for t in plan}
    assert by_handle["alice"].cluster_id == 0
    assert by_handle["bob"].cluster_id == 0
    assert by_handle["carol"].cluster_id == 1


def test_build_plan_keeps_proximity_and_intros_separate(db_conn):
    candidates = [_candidate("target_x", x_followers=15_000)]
    matrix = _matrix(
        rows=["alice", "bob", "carol"],
        follows_map={
            "alice": ["target_x"],
            "bob": ["target_x"],
            "carol": ["something_else"],
        },
    )
    plan = op.build_plan(db_conn, candidates=candidates, co_follow_matrix=matrix)
    target = plan[0]
    assert sorted(target.social_proximity_brokers) == ["alice", "bob"]
    # operator_confirmed_intros is intentionally empty by default — must be
    # populated manually so we don't conflate co-follow with intro willingness.
    assert target.operator_confirmed_intros == []


def test_build_plan_caps_brokers_to_avoid_noisy_lists(db_conn):
    candidates = [_candidate("target_x")]
    rows = [f"k{i}" for i in range(10)]
    follows_map = {r: ["target_x"] for r in rows}
    matrix = _matrix(rows=rows, follows_map=follows_map)
    plan = op.build_plan(db_conn, candidates=candidates, co_follow_matrix=matrix)
    assert len(plan[0].social_proximity_brokers) == 5


def test_build_plan_suggested_theme_is_propagated(db_conn):
    candidates = [_candidate("alice", x_followers=15_000)]
    plan = op.build_plan(
        db_conn,
        candidates=candidates,
        suggested_theme_for_handle={"alice": "RWA fashion / tokenized redemption"},
    )
    assert plan[0].suggested_theme == "RWA fashion / tokenized redemption"


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def test_to_json_payload_groups_by_tier(db_conn):
    candidates = [
        _candidate("megacap", presence={"instagram": {"followers": 500_000}}),
        _candidate("midtier", x_followers=25_000),
        _candidate("smol", x_followers=2_500),
    ]
    plan = op.build_plan(db_conn, candidates=candidates)
    payload = op.to_json_payload(plan)
    assert payload["summary"]["tier_A"] == 1
    assert payload["summary"]["tier_B"] == 1
    assert payload["summary"]["tier_C"] == 1
    assert payload["summary"]["total"] == 3
    assert {t["handle"] for t in payload["targets"]["A"]} == {"megacap"}


def test_to_csv_rows_includes_template_id(db_conn):
    candidates = [_candidate("alice", x_followers=15_000)]
    matrix = _matrix(
        rows=["alice"], follows_map={"alice": ["target"]}
    )
    clusters = [Cluster(cluster_id=4, members=["alice"], label="art curators")]
    plan = op.build_plan(
        db_conn,
        candidates=candidates,
        co_follow_matrix=matrix,
        clusters=clusters,
    )
    rows = op.to_csv_rows(plan)
    assert rows[0]["suggested_template_id"] == "cluster_4"
    assert rows[0]["cluster_label"] == "art curators"


def test_to_csv_rows_default_template_when_no_cluster(db_conn):
    candidates = [_candidate("alice", x_followers=15_000)]
    plan = op.build_plan(db_conn, candidates=candidates)
    rows = op.to_csv_rows(plan)
    assert rows[0]["suggested_template_id"] == "default"
