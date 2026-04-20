"""Vector-based duplicate-candidate generator.

Returns pairs of people/projects pages above a cosine-similarity
threshold. The decision of whether to merge — and the actual merge —
lives in cos (via the ``find_dedup_candidates`` MCP tool + the existing
``update_wiki`` writer). This module is pure candidate generation.

  1. Load QMD embeddings from ~/.cache/qmd/index.sqlite via sqlite-vec
  2. Mean-pool multi-chunk documents so one row == one page
  3. Compute pairwise cosine similarity across people/projects pages
  4. Return the pairs at or above the configured threshold

No LLM calls, no wiki writes. A small, deterministic sweep cos can
call during its reflective pass.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from deja.config import QMD_COLLECTION, QMD_DB_PATH, WIKI_DIR

log = logging.getLogger(__name__)

SIMILARITY_THRESHOLD = 0.82
META_FILES = {"index.md", "log.md", "reflection.md", "goals.md"}


@dataclass
class CandidatePair:
    """One pair of wiki pages above the vector similarity threshold."""

    page_a: str  # "category/slug" (no .md suffix)
    page_b: str
    similarity: float


# ---------------------------------------------------------------------------
# QMD vector load
# ---------------------------------------------------------------------------


def _connect_qmd_db() -> sqlite3.Connection:
    if not QMD_DB_PATH.exists():
        raise RuntimeError(
            f"QMD index not found at {QMD_DB_PATH}. "
            f"Run `qmd embed` against the Deja collection before running dedup."
        )
    try:
        import sqlite_vec  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "sqlite-vec is required for dedup but is not installed. "
            "Add it to pyproject.toml and reinstall the venv."
        ) from e
    db = sqlite3.connect(QMD_DB_PATH)
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    return db


def _load_doc_vectors(db: sqlite3.Connection) -> tuple[list[str], np.ndarray]:
    """Return (paths, L2-normalized-mean-pooled-vectors) for all people/projects pages.

    Mean-pools multi-chunk documents so one row == one page. Raises if the
    QMD collection is empty or missing — a silent empty-return would hide
    a real indexing problem.
    """
    rows = db.execute(
        """
        SELECT d.path, cv.seq, vec_to_json(vv.embedding) AS vec_json
        FROM documents d
        JOIN content_vectors cv ON cv.hash = d.hash
        JOIN vectors_vec vv ON vv.hash_seq = d.hash || '_' || cv.seq
        WHERE d.collection = ?
          AND d.active = 1
          AND (d.path LIKE 'people/%' OR d.path LIKE 'projects/%')
        ORDER BY d.path, cv.seq
        """,
        (QMD_COLLECTION,),
    ).fetchall()

    if not rows:
        raise RuntimeError(
            f"QMD collection {QMD_COLLECTION!r} has no active people/projects "
            f"documents. Run `qmd update && qmd embed` on {WIKI_DIR} before dedup."
        )

    by_path: dict[str, list[list[float]]] = {}
    for path, _seq, vec_json in rows:
        name = Path(path).name
        if name in META_FILES:
            continue
        vec = json.loads(vec_json)
        by_path.setdefault(path, []).append(vec)

    paths = sorted(by_path)
    if not paths:
        raise RuntimeError(
            "All QMD rows for Deja were filtered out as meta files — "
            "nothing to dedup."
        )

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


def _path_to_pageid(path: str) -> str:
    """Convert 'people/foo.md' to 'people/foo'."""
    return path.removesuffix(".md")


# ---------------------------------------------------------------------------
# Candidate detection
# ---------------------------------------------------------------------------


def find_candidates(
    threshold: float = SIMILARITY_THRESHOLD,
    *,
    category: str = "all",
) -> list[CandidatePair]:
    """Return all people/projects page pairs with cosine similarity >= threshold.

    ``category`` restricts the scan to ``"people"`` or ``"projects"``;
    ``"all"`` keeps the default cross-type behavior. Pairs that cross
    the two folders are excluded — dedup only makes sense within a
    category.

    Raises on any failure (missing db, empty collection, sqlite-vec
    missing). The result is sorted by similarity descending.
    """
    db = _connect_qmd_db()
    try:
        paths, mat = _load_doc_vectors(db)
    finally:
        db.close()

    sim = mat @ mat.T
    pairs: list[CandidatePair] = []
    n = len(paths)
    for i in range(n):
        for j in range(i + 1, n):
            s = float(sim[i, j])
            if s < threshold:
                continue
            ip = paths[i]
            jp = paths[j]
            in_people = ip.startswith("people/") and jp.startswith("people/")
            in_projects = ip.startswith("projects/") and jp.startswith("projects/")
            if not (in_people or in_projects):
                continue
            if category == "people" and not in_people:
                continue
            if category == "projects" and not in_projects:
                continue
            pairs.append(
                CandidatePair(
                    page_a=_path_to_pageid(ip),
                    page_b=_path_to_pageid(jp),
                    similarity=s,
                )
            )
    pairs.sort(key=lambda p: p.similarity, reverse=True)
    return pairs


# ---------------------------------------------------------------------------
# Page-snippet helper — shared by MCP candidate tools
# ---------------------------------------------------------------------------


_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)


def load_page_snippet(page_id: str, body_chars: int = 400) -> tuple[str, str, str]:
    """Return (title, frontmatter, compact body snippet) for a page.

    Reads ``<WIKI_DIR>/<page_id>.md`` and collapses whitespace. Used by
    the MCP candidate tools so cos sees enough of each page to decide
    whether to pull the full body via ``get_page``.
    """
    path = WIKI_DIR / f"{page_id}.md"
    if not path.exists():
        return (page_id, "", "")
    raw = path.read_text(encoding="utf-8")
    frontmatter = ""
    body = raw
    m = _FRONTMATTER_RE.match(raw)
    if m:
        frontmatter = m.group(1).strip()
        body = m.group(2).strip()
    title = Path(page_id).name
    for line in body.splitlines():
        if line.startswith("# "):
            title = line[2:].strip()
            break
    body_lines = [ln for ln in body.splitlines() if not ln.startswith("# ")]
    body_flat = re.sub(r"\s+", " ", " ".join(body_lines)).strip()
    return title, frontmatter, body_flat[:body_chars]
