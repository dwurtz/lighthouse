"""Event theme sweep — find recurring themes in events that don't yet
have a project page, and emit proposals so the next integrate cycle
can create the project.

Runs during the 3x/day reflection slot alongside dedup and contradictions.
Reuses the same QMD embeddings dedup already computes — we just change
the filter from people/projects to events/ and look for clusters that
lack a shared project link.

**The sweep does not write to the wiki.** It writes one observation per
cluster to ``observations.jsonl`` with source ``cluster_proposal``. The
next integrate cycle picks these up like any other signal and decides
whether to create the project, link the events, and update goals. Keeps
writes centralized in integrate where all the reasoning already lives.

Why 3+ events, not 2: two related events could be coincidence. Three
is a pattern. This threshold is loose enough that recurring vendors,
ongoing conversations, and repeat activities get surfaced without
every one-off pair becoming a false positive.

Why require `projects: []` on ALL cluster members: if even one event
already has a project, the pattern is already captured — don't propose
a duplicate. Integrate can separately link the unprojected ones to
the existing project via its normal flow.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import yaml

from deja.config import DEJA_HOME, QMD_COLLECTION, WIKI_DIR

log = logging.getLogger(__name__)


# Similarity threshold for event clustering. Lower than dedup's 0.82
# because we're looking for "thematically related" not "same entity" —
# pool-chlorine-issue and pool-payment are about the same service but
# describe different actions.
_SIMILARITY_THRESHOLD = 0.55

# Minimum cluster size before we propose a project. Two events could
# be coincidence; three is a pattern.
_MIN_CLUSTER_SIZE = 3

# Cap on proposals per sweep so a wiki with tons of unprojected events
# doesn't flood integrate with 50 proposals at once.
_MAX_PROPOSALS_PER_SWEEP = 10


@dataclass
class EventCluster:
    paths: list[str]  # e.g. ["events/2026-04-05/foo.md", ...]
    shared_people: list[str]  # slugs common across the cluster
    avg_similarity: float


def _connect_qmd_db() -> sqlite3.Connection:
    """Open the QMD SQLite store — same approach as dedup."""
    from deja.dedup import _connect_qmd_db as _dedup_connect

    return _dedup_connect()


def _load_event_vectors(
    db: sqlite3.Connection,
) -> tuple[list[str], np.ndarray]:
    """Return (paths, L2-normalized mean-pooled vectors) for all event pages.

    Mean-pools multi-chunk documents so one row == one page. Mirrors
    dedup._load_doc_vectors but filters to events/ instead of
    people+projects. Returns ([], empty matrix) if there are fewer than
    _MIN_CLUSTER_SIZE events — nothing to cluster.
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
    if len(paths) < _MIN_CLUSTER_SIZE:
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


def _parse_event_frontmatter(path: str) -> dict:
    """Return the YAML frontmatter dict for an event page, or {}.

    Handles both clean multi-line frontmatter:

        ---
        date: 2026-04-06
        people: [david-wurtz]
        projects: []
        ---

    and the one-line variant that earlier integrate bugs produced:

        ---date: 2026-04-06time: "17:47"people: [david-wurtz]projects: [foo]---

    About 30 events in the current wiki have the broken one-liner
    shape. Silently treating those as empty frontmatter would make
    them ineligible for clustering and could produce false "no
    project yet" proposals when a project link actually exists.
    """
    import re

    fp = WIKI_DIR / path
    if not fp.exists():
        return {}
    try:
        text = fp.read_text(encoding="utf-8")
        if not text.startswith("---"):
            return {}

        # Try the multi-line form first
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
        # Look for the closing --- anywhere on the first line (or any line)
        end_close = text.find("---", 3)
        if end_close == -1:
            return {}
        inline = text[3:end_close]

        # Extract specific fields via regex — we only actually need
        # `people` and `projects` for clustering decisions
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


def _cluster_events(
    paths: list[str],
    mat: np.ndarray,
    threshold: float = _SIMILARITY_THRESHOLD,
) -> list[EventCluster]:
    """Greedy clustering: find groups of 3+ events where every member
    has cosine similarity >= threshold to at least one other member.

    Pre-filters to ONLY events with ``projects: []`` before clustering.
    Clustering across projected + unprojected events produces chains
    where the projected members contaminate the cluster at low
    similarity thresholds, causing the "all unprojected" filter to
    reject everything. Filtering first is simpler and keeps the
    threshold reasonable.

    Returns clusters that also satisfy the "at least one shared person
    slug (non-david)" OR "very high average similarity (>=0.85)"
    criteria. Clusters without a shared anchor AND only moderate
    similarity are too weak to propose.
    """
    # Pre-filter: keep only events without a project
    unprojected_idx: list[int] = []
    unprojected_frontmatters: list[dict] = []
    for i, p in enumerate(paths):
        fm = _parse_event_frontmatter(p)
        if fm.get("projects"):
            continue
        unprojected_idx.append(i)
        unprojected_frontmatters.append(fm)

    if len(unprojected_idx) < _MIN_CLUSTER_SIZE:
        return []

    # Subset the similarity matrix to just unprojected events
    sub_paths = [paths[i] for i in unprojected_idx]
    sub_mat = mat[unprojected_idx]
    sub_sim = sub_mat @ sub_mat.T

    n = len(sub_paths)
    visited = [False] * n
    clusters: list[EventCluster] = []

    for i in range(n):
        if visited[i]:
            continue
        # Greedy expansion
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

        if len(cluster_idx) < _MIN_CLUSTER_SIZE:
            continue

        cluster_paths = [sub_paths[k] for k in cluster_idx]
        frontmatters = [unprojected_frontmatters[k] for k in cluster_idx]
        # Use sub_sim for the within-cluster average below
        sim = sub_sim

        people_sets = [set(fm.get("people") or []) for fm in frontmatters]
        shared = set.intersection(*people_sets) if people_sets else set()
        # Strip the current user's own slug — they're in almost every
        # event and don't carry cluster signal on their own.
        try:
            from deja.identity import load_user

            self_slug = (load_user().slug or "").lower()
            if self_slug:
                shared.discard(self_slug)
        except Exception:
            pass

        # Average pairwise similarity within the cluster — useful for
        # sorting proposals by confidence.
        if len(cluster_idx) == 1:
            avg = 1.0
        else:
            pair_sims = [
                float(sim[a, b])
                for a in cluster_idx
                for b in cluster_idx
                if a < b
            ]
            avg = sum(pair_sims) / len(pair_sims) if pair_sims else 0.0

        # Cluster acceptance rules:
        #   - shared non-david person → accept (recurring vendor/contact)
        #   - OR very high similarity (>=0.85 avg) → accept (the events
        #     are clearly about the same theme even if no common contact
        #     surfaces, e.g. "david studies defensive driving" × 4)
        # Everything else: reject. Low-similarity coincidental groupings
        # make bad project proposals.
        if not shared and avg < 0.85:
            continue

        clusters.append(
            EventCluster(
                paths=cluster_paths,
                shared_people=sorted(shared),
                avg_similarity=avg,
            )
        )

    # Most-confident clusters first
    clusters.sort(key=lambda c: c.avg_similarity, reverse=True)
    return clusters[:_MAX_PROPOSALS_PER_SWEEP]


def _event_titles(paths: list[str]) -> list[tuple[str, str]]:
    """Return (path, h1-title) pairs for display in the proposal."""
    out: list[tuple[str, str]] = []
    for p in paths:
        fp = WIKI_DIR / p
        title = Path(p).stem
        try:
            for line in fp.read_text(encoding="utf-8").splitlines():
                if line.startswith("# "):
                    title = line[2:].strip()
                    break
        except Exception:
            pass
        out.append((p, title))
    return out


def _emit_proposal(cluster: EventCluster) -> None:
    """Append one ``create_project`` observation to observations.jsonl.

    Integrate picks these up like any other signal and decides in its
    next cycle whether to create the project page and link the events.
    """
    titled = _event_titles(cluster.paths)
    body_lines = [
        f"Related events without a project (shared people: "
        f"{', '.join(cluster.shared_people)}):"
    ]
    for path, title in titled:
        body_lines.append(f"- [[{path.removesuffix('.md')}]] — {title}")
    body_lines.append("")
    body_lines.append(
        "Consider creating a project page that links these events. "
        "If the pattern isn't substantive enough to justify its own "
        "project, do nothing — the events stay as they are."
    )

    ts = datetime.now(timezone.utc).isoformat()
    obs = {
        "source": "create_project",
        "sender": "event_theme_sweep",
        "text": "\n".join(body_lines),
        "timestamp": ts,
        "id_key": (
            "create-project-"
            + "-".join(cluster.shared_people[:2])
            + f"-{datetime.now().strftime('%Y%m%d')}"
        ),
    }
    obs_path = DEJA_HOME / "observations.jsonl"
    obs_path.parent.mkdir(parents=True, exist_ok=True)
    with obs_path.open("a") as f:
        f.write(json.dumps(obs) + "\n")
    log.info(
        "event_theme_sweep: proposal written — %d events, shared=%s, sim=%.2f",
        len(cluster.paths),
        ",".join(cluster.shared_people),
        cluster.avg_similarity,
    )


async def run_event_theme_sweep() -> dict:
    """Sweep events for recurring themes and emit project proposals.

    Runs after dedup and contradictions in the 3x/day reflection slot.
    Returns a summary dict for logging. Any failure raises — no silent
    fallbacks per the project's rule.
    """
    db = _connect_qmd_db()
    try:
        paths, mat = _load_event_vectors(db)
    finally:
        db.close()

    if len(paths) < _MIN_CLUSTER_SIZE:
        log.info(
            "event_theme_sweep: only %d events indexed — skipping",
            len(paths),
        )
        return {"events_indexed": len(paths), "proposals": 0}

    clusters = _cluster_events(paths, mat)
    for cluster in clusters:
        _emit_proposal(cluster)

    log.info(
        "event_theme_sweep: %d events → %d proposals",
        len(paths),
        len(clusters),
    )
    return {
        "events_indexed": len(paths),
        "proposals": len(clusters),
        "clusters": [
            {
                "events": c.paths,
                "shared_people": c.shared_people,
                "avg_similarity": c.avg_similarity,
            }
            for c in clusters
        ],
    }
