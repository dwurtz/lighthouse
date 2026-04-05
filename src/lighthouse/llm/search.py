"""QMD-based retrieval — search memory, goals, events for relevant context.

Instead of loading full files into every LLM prompt, we query QMD for
only the relevant chunks. This reduces token usage and improves quality.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


def _run_qmd_json(cmd: list[str], timeout: int) -> list[dict]:
    """Invoke a `qmd ... --json` command and parse the JSON payload.

    Returns an empty list on any failure (subprocess error, non-zero exit,
    malformed JSON). Does NOT raise — callers degrade gracefully.
    """
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(Path.home()),
        )
    except Exception as e:
        log.warning("qmd subprocess failed (%s): %s", cmd[:2], e)
        return []
    if result.returncode != 0 or not result.stdout.strip():
        return []
    try:
        rows = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
    return rows if isinstance(rows, list) else []


def _filter_and_dedup(
    rows: list[dict],
    *,
    limit: int,
    exclude_basenames: set[str],
) -> list[tuple[str, float]]:
    """Turn raw qmd JSON rows into [(uri, score)] with meta-file filtering
    and per-URI deduplication."""
    out: list[tuple[str, float]] = []
    seen: set[str] = set()
    for row in rows:
        uri = row.get("file") or ""
        score = float(row.get("score") or 0.0)
        if not uri or uri in seen:
            continue
        base = uri.rsplit("/", 1)[-1].lower()
        if base in exclude_basenames:
            continue
        seen.add(uri)
        out.append((uri, score))
        if len(out) >= limit:
            break
    return out


def search_files(
    query: str,
    *,
    collection: str | None = None,
    limit: int = 10,
    exclude_basenames: set[str] | None = None,
) -> list[tuple[str, float]]:
    """Return [(qmd_uri, score)] for the top matches of a BM25 search.

    Fast (sub-100ms) — no embedding, no reranking. Catches direct
    keyword overlap between the query and wiki pages. `exclude_basenames`
    filters out meta-files (index.md, log.md, etc.) that always score
    highly because they reference every entity.
    """
    if not query.strip():
        return []
    cmd = ["qmd", "search", query, "--json", "-n", str(limit * 2)]
    if collection:
        cmd += ["--collection", collection]
    rows = _run_qmd_json(cmd, timeout=10)
    return _filter_and_dedup(
        rows,
        limit=limit,
        exclude_basenames={b.lower() for b in (exclude_basenames or set())},
    )


def vsearch_files(
    query: str,
    *,
    collection: str | None = None,
    limit: int = 10,
    exclude_basenames: set[str] | None = None,
) -> list[tuple[str, float]]:
    """Return [(qmd_uri, score)] for the top matches of a vector search.

    Slower than `search_files` (~3s warm) because each call embeds the
    query via the local embedding model, but semantic: catches matches
    where keywords don't overlap (e.g. "children's clothing site" → a
    project page about a "Shopify theme for Blade and Rose"). Pair with
    `search_files` and merge results for recall.

    Requires `qmd embed` to have been run at least once — if the index
    has no vectors this returns an empty list.
    """
    if not query.strip():
        return []
    cmd = ["qmd", "vsearch", query, "--json", "-n", str(limit * 2)]
    if collection:
        cmd += ["--collection", collection]
    # Vector search is slower than BM25; allow more headroom but still
    # bounded so one bad query can't stall the analysis cycle.
    rows = _run_qmd_json(cmd, timeout=30)
    return _filter_and_dedup(
        rows,
        limit=limit,
        exclude_basenames={b.lower() for b in (exclude_basenames or set())},
    )


def search(query: str, limit: int = 5, collection: str | None = None) -> str:
    """Search QMD for relevant context. Returns formatted text.

    If `collection` is provided, only that collection is searched (e.g. "wiki").
    """
    try:
        cmd = ["qmd", "search", query]
        if collection:
            cmd += ["--collection", collection]
        result = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=10,
            cwd=str(Path.home()),
        )
        if result.returncode != 0:
            return ""
        # QMD outputs formatted results — extract the text content
        lines = result.stdout.strip().split("\n")
        chunks = []
        current_chunk = []
        for line in lines:
            if line.startswith("qmd://"):
                if current_chunk:
                    chunks.append("\n".join(current_chunk))
                current_chunk = [line]
            elif line.startswith("@@") or line.startswith("Title:") or line.startswith("Score:"):
                continue
            elif line.strip():
                current_chunk.append(line)
        if current_chunk:
            chunks.append("\n".join(current_chunk))
        return "\n---\n".join(chunks[:limit])
    except Exception as e:
        log.warning("QMD search failed: %s", e)
        return ""


def refresh_index():
    """Re-index QMD collections to pick up new content."""
    try:
        subprocess.run(
            ["qmd", "update"],
            capture_output=True, timeout=15,
            cwd=str(Path.home()),
        )
    except Exception:
        pass


def update_embeddings():
    """Generate/update vector embeddings incrementally. Only call during low-activity periods."""
    try:
        subprocess.run(
            ["qmd", "embed"],
            capture_output=True, timeout=300,  # 5 min max
            cwd=str(Path.home()),
        )
        log.info("QMD embeddings updated")
    except subprocess.TimeoutExpired:
        log.warning("QMD embed timed out after 5 min")
    except Exception:
        pass
