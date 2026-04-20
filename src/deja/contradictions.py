"""Cross-page contradiction candidate generator.

Pages that describe different entities whose content happens to
reference the same subject (a person page vs. a project page that
mentions that person) may drift silently — neither dedup nor normal
integrate co-retrieves them. This module returns clusters of pages in
the contradiction similarity window so cos can inspect them directly.

Reuses dedup's sqlite-vec + mean-pooled QMD embeddings. Walks the
similarity matrix with a *lower* threshold than dedup (dedup uses
≥0.82, so any pair at or above that was already merged or rejected as
a hierarchy). Pairs in the contradiction window (``CONTRA_MIN`` ..
``CONTRA_MAX``) are grouped into small clusters (up to
``MAX_CLUSTER_SIZE`` pages each) and returned.

No LLM calls, no wiki writes. Cos pulls full bodies via ``get_page``
and decides whether there's a real contradiction to resolve.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np

from deja.dedup import (
    _connect_qmd_db,
    _load_doc_vectors,
    _path_to_pageid,
)

log = logging.getLogger(__name__)


# Similarity window for contradiction clustering. Dedup's threshold is
# 0.82, so anything at or above should already be merged (or rejected
# as a hierarchy relationship). We look below that for pairs similar
# enough to talk about the same subject but not similar enough to be
# the same entity.
CONTRA_MIN = 0.65
CONTRA_MAX = 0.82

# Cap cluster size so the payload cos receives stays tractable.
MAX_CLUSTER_SIZE = 6


@dataclass
class ContraCluster:
    """A small group of topically-related pages for cos to inspect."""

    page_ids: list[str]  # ["category/slug", ...]
    mean_similarity: float  # for ordering clusters most-promising-first


# ---------------------------------------------------------------------------
# Clustering — reuse the dedup vector pass, lower threshold
# ---------------------------------------------------------------------------


def _build_clusters(
    paths: list[str],
    mat: np.ndarray,
    sim_min: float = CONTRA_MIN,
    sim_max: float = CONTRA_MAX,
    max_cluster_size: int = MAX_CLUSTER_SIZE,
) -> list[ContraCluster]:
    """Group pages with pairwise similarity in the contradiction window.

    Greedy connected-components walk over the adjacency graph where an
    edge exists iff ``sim_min <= sim < sim_max``. Singletons are
    dropped. Oversized components are split by repeatedly peeling off
    the top-``max_cluster_size`` nodes most similar to the cluster
    centroid.

    Returns clusters sorted by ``mean_similarity`` descending.
    """
    n = len(paths)
    if n < 2:
        return []

    sim = mat @ mat.T
    adj: dict[int, set[int]] = {i: set() for i in range(n)}
    for i in range(n):
        for j in range(i + 1, n):
            s = float(sim[i, j])
            if sim_min <= s < sim_max:
                adj[i].add(j)
                adj[j].add(i)

    seen: set[int] = set()
    components: list[list[int]] = []
    for start in range(n):
        if start in seen or not adj[start]:
            continue
        stack = [start]
        comp: list[int] = []
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            comp.append(node)
            stack.extend(adj[node] - seen)
        if len(comp) >= 2:
            components.append(sorted(comp))

    clusters: list[ContraCluster] = []
    for comp in components:
        remaining = list(comp)
        while remaining:
            if len(remaining) <= max_cluster_size:
                pick = remaining
                remaining = []
            else:
                sub_mat = mat[remaining]
                centroid = sub_mat.mean(axis=0)
                norm = np.linalg.norm(centroid)
                if norm > 0:
                    centroid = centroid / norm
                scores = sub_mat @ centroid
                order = np.argsort(-scores)
                pick_idx = order[:max_cluster_size]
                keep_idx = order[max_cluster_size:]
                pick = [remaining[int(k)] for k in pick_idx]
                remaining = [remaining[int(k)] for k in keep_idx]

            if len(pick) < 2:
                continue

            page_ids = [_path_to_pageid(paths[i]) for i in pick]
            pair_vals: list[float] = []
            for a_idx in range(len(pick)):
                for b_idx in range(a_idx + 1, len(pick)):
                    pair_vals.append(float(sim[pick[a_idx], pick[b_idx]]))
            mean_sim = sum(pair_vals) / len(pair_vals) if pair_vals else 0.0
            clusters.append(ContraCluster(page_ids=page_ids, mean_similarity=mean_sim))

    clusters.sort(key=lambda c: c.mean_similarity, reverse=True)
    return clusters


def find_contradiction_clusters(
    sim_min: float = CONTRA_MIN,
    sim_max: float = CONTRA_MAX,
    max_cluster_size: int = MAX_CLUSTER_SIZE,
) -> list[ContraCluster]:
    """Return the ranked list of contradiction-window clusters.

    Wraps dedup's sqlite-vec loader. Raises on any infra failure (missing
    db, empty collection, sqlite-vec not installed) — the same loud-fail
    posture dedup uses.
    """
    db = _connect_qmd_db()
    try:
        paths, mat = _load_doc_vectors(db)
    finally:
        db.close()
    return _build_clusters(
        paths, mat,
        sim_min=sim_min, sim_max=sim_max,
        max_cluster_size=max_cluster_size,
    )
