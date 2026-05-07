"""Tests for sable_kol.follow_graph — co-follow matrix, kingmakers, clusters."""
from __future__ import annotations

from typing import Any

import pytest

from sable_kol import follow_graph as fg


# ---------------------------------------------------------------------------
# Synthetic-fixture helper — populate kol_extract_runs + kol_follow_edges.
# ---------------------------------------------------------------------------

def _seed_run(conn, *, run_id: str, target_handle: str, completed: int = 1) -> None:
    conn.execute(
        "INSERT INTO kol_extract_runs "
        "(run_id, target_handle_normalized, provider, extract_type, cursor_completed) "
        "VALUES (:r, :h, 'socialdata', 'following', :c)",
        {"r": run_id, "h": target_handle, "c": completed},
    )
    conn.commit()


def _seed_edges(conn, *, run_id: str, edges: list[tuple[str, str]]) -> None:
    for follower_handle, followed_handle in edges:
        conn.execute(
            "INSERT INTO kol_follow_edges "
            "(run_id, follower_id, follower_handle, followed_id, followed_handle) "
            "VALUES (:r, :fi, :fh, :di, :dh)",
            {
                "r": run_id,
                "fi": f"id_{follower_handle}",
                "fh": follower_handle,
                "di": f"id_{followed_handle}",
                "dh": followed_handle,
            },
        )
    conn.commit()


def _seed_following_for(conn, *, kol_handle: str, follows: list[str], completed: int = 1) -> None:
    """Convenience: a single 'following' extract for kol_handle → follows."""
    rid = f"run_{kol_handle}"
    _seed_run(conn, run_id=rid, target_handle=kol_handle, completed=completed)
    _seed_edges(
        conn,
        run_id=rid,
        edges=[(kol_handle, target) for target in follows],
    )


# ---------------------------------------------------------------------------
# build_co_follow_matrix
# ---------------------------------------------------------------------------

def test_matrix_uses_completed_runs_only(db_conn):
    # Completed run for alice, partial run for bob.
    _seed_following_for(db_conn, kol_handle="alice", follows=["x", "y"], completed=1)
    _seed_following_for(db_conn, kol_handle="bob", follows=["x", "z"], completed=0)
    m = fg.build_co_follow_matrix(db_conn)
    assert m.rows == ["alice"]
    # bob's edges excluded; only alice's columns should appear.
    assert sorted(m.cols) == ["x", "y"]


def test_matrix_orientation_following(db_conn):
    """For extract_type='following', rows = the kol whose graph we pulled."""
    _seed_following_for(db_conn, kol_handle="alice", follows=["x", "y"])
    _seed_following_for(db_conn, kol_handle="bob", follows=["x", "z"])
    m = fg.build_co_follow_matrix(db_conn)
    assert sorted(m.rows) == ["alice", "bob"]
    assert sorted(m.cols) == ["x", "y", "z"]


def test_matrix_handle_filter_restricts_rows(db_conn):
    _seed_following_for(db_conn, kol_handle="alice", follows=["x"])
    _seed_following_for(db_conn, kol_handle="bob", follows=["x"])
    _seed_following_for(db_conn, kol_handle="carol", follows=["x"])
    m = fg.build_co_follow_matrix(db_conn, kol_handles=["alice", "bob"])
    assert sorted(m.rows) == ["alice", "bob"]


def test_matrix_empty_when_no_completed_edges(db_conn):
    _seed_following_for(db_conn, kol_handle="alice", follows=["x"], completed=0)
    m = fg.build_co_follow_matrix(db_conn)
    assert m.rows == []
    assert m.cols == []


# ---------------------------------------------------------------------------
# identify_kingmakers
# ---------------------------------------------------------------------------

def test_kingmakers_returns_columns_above_min_count(db_conn):
    # 5 KOLs all follow @kingmaker_a; 2 follow @noise.
    for kol in ["k1", "k2", "k3", "k4", "k5"]:
        _seed_following_for(db_conn, kol_handle=kol, follows=["kingmaker_a"])
    for kol in ["k1", "k2"]:
        _seed_edges(
            db_conn, run_id=f"run_{kol}", edges=[(kol, "noise")]
        )
    m = fg.build_co_follow_matrix(db_conn)
    kings = fg.identify_kingmakers(m, min_count=3)
    assert len(kings) == 1
    assert kings[0].handle == "kingmaker_a"
    assert kings[0].follower_count_in_pool == 5


def test_kingmakers_sorted_by_count_descending(db_conn):
    # popular_one is followed by all three KOLs; popular_two by only two.
    _seed_following_for(db_conn, kol_handle="a", follows=["popular_one", "popular_two"])
    _seed_following_for(db_conn, kol_handle="b", follows=["popular_one", "popular_two"])
    _seed_following_for(db_conn, kol_handle="c", follows=["popular_one"])
    m = fg.build_co_follow_matrix(db_conn)
    kings = fg.identify_kingmakers(m, min_count=2)
    handles = [k.handle for k in kings]
    assert handles[0] == "popular_one"
    assert handles[1] == "popular_two"
    assert kings[0].follower_count_in_pool == 3
    assert kings[1].follower_count_in_pool == 2


# ---------------------------------------------------------------------------
# cluster_kols — multi-threshold + connected components
# ---------------------------------------------------------------------------

def test_cluster_kols_groups_high_similarity_kols(db_conn):
    """Two cliques of 5 KOLs each, no overlap → two clusters."""
    clique_one_targets = ["t1", "t2", "t3", "t4"]
    clique_two_targets = ["u1", "u2", "u3", "u4"]
    for kol in ["a1", "a2", "a3", "a4", "a5"]:
        _seed_following_for(db_conn, kol_handle=kol, follows=clique_one_targets)
    for kol in ["b1", "b2", "b3", "b4", "b5"]:
        _seed_following_for(db_conn, kol_handle=kol, follows=clique_two_targets)

    m = fg.build_co_follow_matrix(db_conn)
    out = fg.cluster_kols(m, thresholds=(0.5,))
    clusters = out[0.5]
    sizes = sorted([len(c.members) for c in clusters], reverse=True)
    # Two clusters of 5 each (the two cliques), nothing else.
    assert sizes == [5, 5]


def test_cluster_kols_singletons_at_high_threshold(db_conn):
    # Each KOL has unique follows → no edges at threshold 0.5.
    _seed_following_for(db_conn, kol_handle="alice", follows=["only_a"])
    _seed_following_for(db_conn, kol_handle="bob", follows=["only_b"])
    _seed_following_for(db_conn, kol_handle="carol", follows=["only_c"])
    m = fg.build_co_follow_matrix(db_conn)
    out = fg.cluster_kols(m, thresholds=(0.5,))
    clusters = out[0.5]
    assert len(clusters) == 3  # all singletons
    for c in clusters:
        assert len(c.members) == 1


def test_cluster_kols_multi_threshold_returns_dict(db_conn):
    for kol in ["a", "b", "c"]:
        _seed_following_for(db_conn, kol_handle=kol, follows=["shared", f"unique_{kol}"])
    m = fg.build_co_follow_matrix(db_conn)
    out = fg.cluster_kols(m, thresholds=(0.05, 0.15, 0.30))
    assert set(out.keys()) == {0.05, 0.15, 0.30}


# ---------------------------------------------------------------------------
# cluster_label_via_tfidf
# ---------------------------------------------------------------------------

def test_label_surfaces_cluster_distinguishing_handle(db_conn):
    # Cluster A: a1, a2 follow rare_to_a + universal.
    # Cluster B: b1 follows only universal.
    _seed_following_for(db_conn, kol_handle="a1", follows=["rare_to_a", "universal"])
    _seed_following_for(db_conn, kol_handle="a2", follows=["rare_to_a", "universal"])
    _seed_following_for(db_conn, kol_handle="b1", follows=["universal"])

    m = fg.build_co_follow_matrix(db_conn)
    label = fg.cluster_label_via_tfidf(["a1", "a2"], m, top_k=3)
    # rare_to_a should dominate over universal because it's distinguishing.
    label_handles = [s.strip() for s in label.split(",")]
    assert label_handles[0] == "rare_to_a"


# ---------------------------------------------------------------------------
# map_social_proximity
# ---------------------------------------------------------------------------

def test_proximity_only_returns_brokers_in_pool(db_conn):
    _seed_following_for(db_conn, kol_handle="alice", follows=["target_x"])
    _seed_following_for(db_conn, kol_handle="bob", follows=["target_x"])
    _seed_following_for(db_conn, kol_handle="carol", follows=["target_x"])
    m = fg.build_co_follow_matrix(db_conn)
    res = fg.map_social_proximity("target_x", ["alice", "bob"], m)
    assert sorted(res.brokers) == ["alice", "bob"]
    # carol follows target_x but is not in the pool.
    assert "carol" not in res.brokers


def test_proximity_excludes_target_self(db_conn):
    _seed_following_for(db_conn, kol_handle="alice", follows=["alice"])  # weird but possible
    _seed_following_for(db_conn, kol_handle="bob", follows=["alice"])
    m = fg.build_co_follow_matrix(db_conn)
    res = fg.map_social_proximity("alice", ["alice", "bob"], m)
    assert res.brokers == ["bob"]


def test_proximity_handles_unknown_target(db_conn):
    _seed_following_for(db_conn, kol_handle="alice", follows=["target_x"])
    m = fg.build_co_follow_matrix(db_conn)
    res = fg.map_social_proximity("not_in_graph", ["alice"], m)
    assert res.brokers == []
