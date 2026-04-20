"""Event-cluster candidate generator.

Groups events that look like they belong to a not-yet-existing project,
using two deterministic mechanisms:

  - *Dangling-slug clusters*: events whose ``projects:`` frontmatter
    references a slug that has no corresponding ``projects/<slug>.md``
    page. When ≥2 events share the same dangling slug, the events are
    already voting for the slug and the slug is the name.

  - *Vector-similarity clusters*: events with an empty ``projects:``
    field that cluster via QMD embeddings at similarity ≥0.55 AND
    share a non-user person OR have average similarity ≥0.85.

When both mechanisms surface the same event group, the dangling-slug
variant wins — slug is pre-named and deterministic.

No LLM calls, no wiki writes. Cos consumes this via the
``find_orphan_event_clusters`` MCP tool and decides whether to
materialize a project page (via ``update_wiki``).
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import yaml

from deja.config import QMD_COLLECTION, WIKI_DIR

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Similarity threshold for event clustering. Lower than dedup's 0.82
# because we're looking for "thematically related" not "same entity" —
# pool-chlorine-issue and pool-payment are about the same service but
# describe different actions.
_SIMILARITY_THRESHOLD = 0.55

# Minimum cluster size for vector-similarity clusters. Two events could
# be coincidence; three is a pattern.
_MIN_CLUSTER_SIZE = 3

# Minimum number of events voting for the same dangling slug. The slug
# IS a stronger signal than vectors alone — two events literally saying
# "we belong to the same (non-existent) project" is enough.
_MIN_DANGLING_VOTES = 2


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class EventCluster:
    """One candidate cluster to hand to cos.

    ``suggested_slug`` is set only for dangling-slug clusters. For vector
    clusters it's None — cos invents the slug. ``source`` distinguishes
    the two mechanisms in logs + audit reasons.
    """

    cluster_id: str
    paths: list[str]  # "events/YYYY-MM-DD/slug.md"
    shared_people: list[str]
    avg_similarity: float
    suggested_slug: str | None = None
    source: str = "vector"  # "dangling" | "vector"


# ---------------------------------------------------------------------------
# QMD vector load — reuses dedup's connection helper
# ---------------------------------------------------------------------------


def _connect_qmd_db() -> sqlite3.Connection:
    """Open the QMD SQLite store — same approach as dedup."""
    from deja.dedup import _connect_qmd_db as _dedup_connect

    return _dedup_connect()


def _load_event_vectors(
    db: sqlite3.Connection,
) -> tuple[list[str], np.ndarray]:
    """Return (paths, L2-normalized mean-pooled vectors) for all event pages.

    Mean-pools multi-chunk documents so one row == one page. Returns
    ([], empty matrix) if there are too few events to cluster.
    """
    rows = db.execute(
        """
        SELECT d.path, cv.seq, vec_to_json(vv.embedding) AS vec_json
        FROM documents d
        JOIN content_vectors cv ON cv.hash = d.hash
        JOIN vectors_vec vv ON vv.hash_seq = d.hash || '_' || cv.seq
        WHERE d.collection = ?
          AND d.active = 1
          AND d.path LIKE 'events/%'
        ORDER BY d.path, cv.seq
        """,
        (QMD_COLLECTION,),
    ).fetchall()

    if not rows:
        return [], np.zeros((0, 0), dtype=np.float32)

    by_path: dict[str, list[list[float]]] = {}
    for path, _seq, vec_json in rows:
        vec = json.loads(vec_json)
        by_path.setdefault(path, []).append(vec)

    paths = sorted(by_path)
    if not paths:
        return [], np.zeros((0, 0), dtype=np.float32)

    dim = len(by_path[paths[0]][0])
    mat = np.zeros((len(paths), dim), dtype=np.float32)
    for i, p in enumerate(paths):
        chunks = np.array(by_path[p], dtype=np.float32)
        pooled = chunks.mean(axis=0)
        norm = np.linalg.norm(pooled)
        if norm > 0:
            pooled = pooled / norm
        mat[i] = pooled
    return paths, mat


# ---------------------------------------------------------------------------
# Event frontmatter + existing-project enumeration
# ---------------------------------------------------------------------------


def _parse_event_frontmatter(path: str) -> dict:
    """Return the YAML frontmatter dict for an event page, or {}.

    Handles the clean multi-line form and the legacy one-line corruption
    shape (some events in the current wiki still have it). Only
    ``people`` and ``projects`` are required for clustering decisions,
    so the one-line fallback uses targeted regex rather than full YAML
    parsing.
    """
    fp = WIKI_DIR / path
    if not fp.exists():
        return {}
    try:
        text = fp.read_text(encoding="utf-8")
        if not text.startswith("---"):
            return {}

        # Try the multi-line form first.
        end = text.find("\n---", 3)
        if end != -1:
            body = text[3:end].strip()
            if body:
                try:
                    parsed = yaml.safe_load(body)
                    if isinstance(parsed, dict):
                        return parsed
                except yaml.YAMLError:
                    pass  # fall through to one-line regex

        # One-line fallback: ---key1: v1 key2: v2...---
        end_close = text.find("---", 3)
        if end_close == -1:
            return {}
        inline = text[3:end_close]

        out: dict = {}
        m = re.search(r"people:\s*\[([^\]]*)\]", inline)
        if m:
            raw = m.group(1).strip()
            out["people"] = [s.strip() for s in raw.split(",") if s.strip()]
        m = re.search(r"projects:\s*\[([^\]]*)\]", inline)
        if m:
            raw = m.group(1).strip()
            out["projects"] = [s.strip() for s in raw.split(",") if s.strip()]
        return out
    except Exception:
        return {}


def _existing_project_slugs() -> set[str]:
    """Return the set of slugs for all existing projects/<slug>.md."""
    projects_dir = WIKI_DIR / "projects"
    if not projects_dir.exists():
        return set()
    return {p.stem for p in projects_dir.glob("*.md")}


def _self_slug() -> str:
    try:
        from deja.identity import load_user

        return (load_user().slug or "").lower()
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Clustering — dangling-slug + vector
# ---------------------------------------------------------------------------


def _find_dangling_clusters(
    paths: list[str],
    existing: set[str],
    self_slug: str,
    min_votes: int = _MIN_DANGLING_VOTES,
) -> list[EventCluster]:
    """Group events by shared dangling-slug references.

    For every event, inspect its ``projects:`` frontmatter. Any slug NOT
    in ``existing`` is "dangling" — the event is voting for a project
    that doesn't exist yet. ≥``min_votes`` events sharing the same
    dangling slug form a cluster.
    """
    slug_to_events: dict[str, list[str]] = {}
    slug_to_people: dict[str, list[set[str]]] = {}
    for path in paths:
        fm = _parse_event_frontmatter(path)
        projects = fm.get("projects") or []
        if not isinstance(projects, list):
            continue
        for slug in projects:
            slug = str(slug).strip()
            if not slug or slug in existing:
                continue
            slug_to_events.setdefault(slug, []).append(path)
            people = set(fm.get("people") or [])
            people.discard(self_slug)
            slug_to_people.setdefault(slug, []).append(people)

    clusters: list[EventCluster] = []
    for slug, evs in slug_to_events.items():
        if len(evs) < min_votes:
            continue
        people_sets = slug_to_people.get(slug) or []
        shared = (
            set.intersection(*people_sets) if people_sets else set()
        )
        clusters.append(
            EventCluster(
                cluster_id=f"dangling-{slug}",
                paths=sorted(evs),
                shared_people=sorted(shared),
                avg_similarity=1.0,  # not meaningful for dangling clusters
                suggested_slug=slug,
                source="dangling",
            )
        )
    clusters.sort(key=lambda c: (-len(c.paths), c.suggested_slug or ""))
    return clusters


def _find_vector_clusters(
    paths: list[str],
    mat: np.ndarray,
    self_slug: str,
    already_clustered: set[str],
    threshold: float = _SIMILARITY_THRESHOLD,
    min_size: int = _MIN_CLUSTER_SIZE,
) -> list[EventCluster]:
    """Greedy clustering on empty-projects events.

    Only considers events with ``projects: []`` (or no projects key) AND
    not already in a dangling cluster. Returns clusters of ≥``min_size``
    that also have a shared non-user person OR high (≥0.85) avg similarity.
    """
    if len(paths) < min_size or mat.size == 0:
        return []

    eligible_idx: list[int] = []
    eligible_fms: list[dict] = []
    for i, p in enumerate(paths):
        if p in already_clustered:
            continue
        fm = _parse_event_frontmatter(p)
        if fm.get("projects"):
            continue
        eligible_idx.append(i)
        eligible_fms.append(fm)

    if len(eligible_idx) < min_size:
        return []

    sub_paths = [paths[i] for i in eligible_idx]
    sub_mat = mat[eligible_idx]
    sub_sim = sub_mat @ sub_mat.T

    n = len(sub_paths)
    visited = [False] * n
    clusters: list[EventCluster] = []
    cluster_counter = 0

    for i in range(n):
        if visited[i]:
            continue
        cluster_idx = [i]
        visited[i] = True
        changed = True
        while changed:
            changed = False
            for j in range(n):
                if visited[j]:
                    continue
                if any(sub_sim[j, k] >= threshold for k in cluster_idx):
                    cluster_idx.append(j)
                    visited[j] = True
                    changed = True

        if len(cluster_idx) < min_size:
            continue

        cluster_paths = [sub_paths[k] for k in cluster_idx]
        fms = [eligible_fms[k] for k in cluster_idx]
        people_sets = [set(fm.get("people") or []) for fm in fms]
        shared = set.intersection(*people_sets) if people_sets else set()
        if self_slug:
            shared.discard(self_slug)

        if len(cluster_idx) == 1:
            avg = 1.0
        else:
            pair_sims = [
                float(sub_sim[a, b])
                for a in cluster_idx
                for b in cluster_idx
                if a < b
            ]
            avg = sum(pair_sims) / len(pair_sims) if pair_sims else 0.0

        if not shared and avg < 0.85:
            continue

        clusters.append(
            EventCluster(
                cluster_id=f"vector-{cluster_counter}",
                paths=sorted(cluster_paths),
                shared_people=sorted(shared),
                avg_similarity=avg,
                suggested_slug=None,
                source="vector",
            )
        )
        cluster_counter += 1

    clusters.sort(key=lambda c: c.avg_similarity, reverse=True)
    return clusters


def find_clusters(
    min_size: int = _MIN_CLUSTER_SIZE,
    sim_threshold: float = _SIMILARITY_THRESHOLD,
) -> tuple[list[EventCluster], int, int, int]:
    """Return (clusters, events_indexed, dangling_count, vector_count).

    Combines dangling-slug + vector mechanisms. Dangling clusters take
    precedence: an event captured by a dangling-slug cluster is NOT
    re-eligible for a vector cluster on the same sweep.
    """
    db = _connect_qmd_db()
    try:
        paths, mat = _load_event_vectors(db)
    finally:
        db.close()

    existing = _existing_project_slugs()
    self_slug = _self_slug()

    dangling = _find_dangling_clusters(paths, existing, self_slug)
    already: set[str] = set()
    for c in dangling:
        already.update(c.paths)

    vector = _find_vector_clusters(
        paths, mat, self_slug, already,
        threshold=sim_threshold, min_size=min_size,
    )

    combined = dangling + vector
    return combined, len(paths), len(dangling), len(vector)
