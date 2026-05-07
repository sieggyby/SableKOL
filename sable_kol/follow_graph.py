"""Follow-graph analysis over ``kol_follow_edges`` (sable.db migration 037).

Computes the artifacts the SolStitch outreach plan Phase 7 needs:

* **Co-follow matrix** — sparse binary representation. Rows are the entities
  whose graphs were extracted (KOLs, when ``extract_type='following'``);
  columns are the accounts they follow.
* **Kingmakers** — accounts followed by at least ``min_count`` of the input
  KOLs. These are the high-leverage standing targets.
* **Clusters** — Jaccard-similarity clustering at multiple thresholds so the
  operator can pick the cut that produces the most-actionable group count.
  Cluster names come from TF-IDF over each cluster's union-of-follows vs the
  full corpus, surfacing what's *distinguishing* about the cluster rather
  than what's just popular overall.
* **Social-proximity brokers** — for a given target, the input KOLs who
  follow that target on X. **Co-follow ≠ willingness to introduce.** Use this
  signal as a prior, not a commitment. Operator-confirmed intros live in a
  separate field on :class:`OutreachTarget`.

Only edges from runs with ``cursor_completed=1`` participate in any of the
above. Partial extractions are excluded so a 50%-fetched following list does
not contaminate kingmaker counts or cluster centrality.

Implementation: pure-Python sets/dicts. The expected workload (≤200 KOLs ×
~500 followings each = ~100K edges, ~20K pairwise Jaccards) runs in well
under a second without numpy/scipy. A future scale-up can drop scipy in
without changing the public surface.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------

@dataclass
class CoFollowMatrix:
    """Sparse binary co-follow representation, pure-Python.

    ``rows[i]`` is the handle of the ``i``-th KOL whose following list we have.
    ``cols[j]`` is the handle of the ``j``-th followed account.
    ``follows_by_row[i]`` is the set of column-indices that row ``i`` follows.
    """
    rows: list[str]
    cols: list[str]
    follows_by_row: list[set[int]]
    # Kept for API ergonomics; pure-python implementation is never "sparse" in
    # the scipy sense but it IS sparse in shape (we store only the 1s).
    is_sparse: bool = True

    @property
    def matrix(self) -> Any:
        """For diagnostic-only access: returns this object so callers that
        previously poked at .matrix.shape / .nnz still get something usable.
        """
        return self

    @property
    def nnz(self) -> int:
        return sum(len(s) for s in self.follows_by_row)

    @property
    def shape(self) -> tuple[int, int]:
        return (len(self.rows), len(self.cols))


@dataclass
class Kingmaker:
    handle: str
    follower_count_in_pool: int  # how many KOLs in the pool follow this handle


@dataclass
class Cluster:
    cluster_id: int
    members: list[str]
    label: str = ""


# ---------------------------------------------------------------------------
# Build the matrix
# ---------------------------------------------------------------------------

def build_co_follow_matrix(
    conn: Any,
    *,
    kol_handles: list[str] | None = None,
    extract_type: str = "following",
) -> CoFollowMatrix:
    """Read kol_follow_edges (filtered to completed runs) and build the matrix.

    ``extract_type='following'`` is the typical case: rows are the KOLs we
    extracted from (the entity whose graph we pulled), cols are the accounts
    they follow. For ``extract_type='followers'`` the orientation flips —
    rows are the followers (the audience), cols are the targets.

    ``kol_handles`` optionally restricts to a curated set; matched against
    the row dimension.
    """
    rows_list: list[str] = []
    rows_index: dict[str, int] = {}
    cols_list: list[str] = []
    cols_index: dict[str, int] = {}
    follows: list[set[int]] = []

    handle_filter: set[str] | None = None
    if kol_handles is not None:
        handle_filter = {h.lstrip("@").lower().strip() for h in kol_handles}

    sql = (
        "SELECT e.follower_id, e.follower_handle, e.followed_id, e.followed_handle, "
        "       r.target_handle_normalized, r.extract_type "
        "FROM kol_follow_edges e "
        "JOIN kol_extract_runs r ON r.run_id = e.run_id "
        "WHERE r.cursor_completed = 1 AND r.extract_type = :et"
    )
    edges = conn.execute(sql, {"et": extract_type}).fetchall()

    for edge in edges:
        if extract_type == "following":
            row_handle = (edge["target_handle_normalized"] or "").lower().strip()
            col_handle = (edge["followed_handle"] or "").lower().strip()
        else:  # followers
            row_handle = (edge["follower_handle"] or "").lower().strip()
            col_handle = (edge["target_handle_normalized"] or "").lower().strip()

        if not row_handle or not col_handle:
            continue
        if handle_filter is not None and row_handle not in handle_filter:
            continue

        if row_handle not in rows_index:
            rows_index[row_handle] = len(rows_list)
            rows_list.append(row_handle)
            follows.append(set())
        if col_handle not in cols_index:
            cols_index[col_handle] = len(cols_list)
            cols_list.append(col_handle)
        follows[rows_index[row_handle]].add(cols_index[col_handle])

    return CoFollowMatrix(rows=rows_list, cols=cols_list, follows_by_row=follows)


# ---------------------------------------------------------------------------
# Kingmakers
# ---------------------------------------------------------------------------

def identify_kingmakers(
    m: CoFollowMatrix,
    *,
    min_count: int = 30,
) -> list[Kingmaker]:
    """Return columns followed by at least ``min_count`` rows of the matrix."""
    if not m.cols or not m.rows:
        return []
    col_counts: list[int] = [0] * len(m.cols)
    for s in m.follows_by_row:
        for c in s:
            col_counts[c] += 1
    out: list[Kingmaker] = []
    for j, count in enumerate(col_counts):
        if count >= min_count:
            out.append(Kingmaker(handle=m.cols[j], follower_count_in_pool=count))
    out.sort(key=lambda k: (-k.follower_count_in_pool, k.handle))
    return out


# ---------------------------------------------------------------------------
# Clustering (Jaccard, multi-threshold, connected-components)
# ---------------------------------------------------------------------------

def cluster_kols(
    m: CoFollowMatrix,
    *,
    thresholds: tuple[float, ...] = (0.05, 0.15, 0.30),
) -> dict[float, list[Cluster]]:
    """Cluster the rows of ``m`` by Jaccard similarity at each threshold.

    Returns ``{threshold: [Cluster, ...]}``. Singletons are returned as their
    own clusters so ``len(rows)`` is conserved.
    """
    n = len(m.rows)
    if n == 0:
        return {t: [] for t in thresholds}

    sims = _pairwise_jaccard(m)
    out: dict[float, list[Cluster]] = {}
    for t in thresholds:
        adjacency: dict[int, set[int]] = defaultdict(set)
        for i in range(n):
            adjacency[i]  # ensure singletons appear
        for (i, j), s in sims.items():
            if s >= t:
                adjacency[i].add(j)
                adjacency[j].add(i)
        clusters = _connected_components(adjacency, n)
        out[t] = [
            Cluster(cluster_id=cid, members=[m.rows[i] for i in sorted(members)])
            for cid, members in enumerate(clusters)
        ]
    return out


def _pairwise_jaccard(m: CoFollowMatrix) -> dict[tuple[int, int], float]:
    n = len(m.rows)
    out: dict[tuple[int, int], float] = {}
    for i in range(n):
        si = m.follows_by_row[i]
        if not si:
            continue
        for j in range(i + 1, n):
            sj = m.follows_by_row[j]
            if not sj:
                continue
            inter = len(si & sj)
            if inter == 0:
                continue
            union = len(si | sj)
            out[(i, j)] = inter / union
    return out


def _connected_components(adjacency: dict[int, set[int]], n: int) -> list[list[int]]:
    seen: set[int] = set()
    components: list[list[int]] = []
    for start in range(n):
        if start in seen:
            continue
        stack = [start]
        component: list[int] = []
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            component.append(node)
            stack.extend(adjacency.get(node, ()))
        components.append(component)
    return components


# ---------------------------------------------------------------------------
# Cluster naming via TF-IDF over corpus
# ---------------------------------------------------------------------------

def cluster_label_via_tfidf(
    cluster_members: list[str],
    m: CoFollowMatrix,
    *,
    top_k: int = 4,
) -> str:
    """Name a cluster by its most distinguishing followed-handles.

    For each followed handle, score = ``cluster_share * idf`` where
    ``cluster_share`` is the fraction of cluster members who follow the
    handle and ``idf = log((N+1) / corpus_count) + 1``. Surfaces what's
    characteristic of the cluster vs universally popular.
    """
    if not cluster_members or not m.cols or not m.rows:
        return ""

    member_set = {h.lstrip("@").lower().strip() for h in cluster_members}
    member_indices = [i for i, h in enumerate(m.rows) if h in member_set]
    if not member_indices:
        return ""

    n_total = len(m.rows)
    n_cluster = len(member_indices)

    # Counts per column.
    cluster_counts = [0] * len(m.cols)
    corpus_counts = [0] * len(m.cols)
    for i, s in enumerate(m.follows_by_row):
        is_cluster = i in set(member_indices)
        for c in s:
            corpus_counts[c] += 1
            if is_cluster:
                cluster_counts[c] += 1

    scored: list[tuple[float, str]] = []
    for j, col_handle in enumerate(m.cols):
        cc = cluster_counts[j]
        if cc == 0:
            continue
        share = cc / n_cluster
        corpus = corpus_counts[j] or 1
        idf = math.log((n_total + 1) / corpus) + 1.0
        scored.append((share * idf, col_handle))

    if not scored:
        return ""
    scored.sort(reverse=True)
    return ", ".join(handle for _, handle in scored[:top_k])


# ---------------------------------------------------------------------------
# Social-proximity broker mapping
# ---------------------------------------------------------------------------

@dataclass
class SocialProximityResult:
    target_handle: str
    brokers: list[str]


def map_social_proximity(
    target_handle: str,
    kol_pool: list[str],
    m: CoFollowMatrix,
) -> SocialProximityResult:
    """For a target, return KOLs in the pool whose extracted followings include it.

    This is **social-proximity ONLY**: co-follow on X. It does NOT imply
    willingness to make an intro, prior conversation, or any
    operator-confirmed relationship. The :class:`OutreachTarget` dataclass
    in ``outreach_plan.py`` keeps a separate ``operator_confirmed_intros``
    field for actual intro paths, populated via manual operator annotation.
    """
    target = target_handle.lstrip("@").lower().strip()
    pool = {h.lstrip("@").lower().strip() for h in kol_pool}
    cols_lower = [c.lower() for c in m.cols]
    if target not in cols_lower:
        return SocialProximityResult(target_handle=target, brokers=[])
    col_idx = cols_lower.index(target)
    brokers: list[str] = []
    for i, follows in enumerate(m.follows_by_row):
        if col_idx not in follows:
            continue
        kol = m.rows[i]
        if kol in pool and kol != target:
            brokers.append(kol)
    return SocialProximityResult(target_handle=target, brokers=sorted(brokers))
