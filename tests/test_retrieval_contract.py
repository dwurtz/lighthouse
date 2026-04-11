"""Retrieval contract tests — catches the "wiki_retriever silent" class of bug.

wiki_retriever passed ``collection="wiki"`` to QMD for months while the
actual collection on disk was ``"Deja"``. Every integrate cycle silently
retrieved zero pages and fell back to index-only context. No test caught
it because no test asserted that retrieval ACTUALLY returns non-empty
results against a real QMD index.

These tests seed a tiny in-temp wiki, index it with QMD under the same
collection constant the production code uses, and verify that the
retrieval layer returns what it's supposed to. The key invariant they
lock in: **the constant name and the on-disk collection name must match
or every caller returns empty**.

The tests skip gracefully if the ``qmd`` binary isn't on PATH so CI
machines without QMD installed don't fail the suite. Locally the qmd
binary is available and these tests run as the canary for collection
drift.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from deja.config import QMD_COLLECTION, QMD_DB_PATH


_QMD_AVAILABLE = shutil.which("qmd") is not None

pytestmark = pytest.mark.skipif(
    not _QMD_AVAILABLE,
    reason="qmd binary not on PATH — install qmd to run retrieval contract tests",
)


def _seed_wiki_and_index(wiki_home, qmd_cache):
    """Write a tiny fake wiki, point qmd at it, and build an index.

    The wiki has to live somewhere qmd can actually read — qmd defaults
    to indexing the current working directory, so we seed the temp wiki
    and run ``qmd update`` from there. Returns the sqlite DB path qmd
    wrote to (inside ``qmd_cache``).
    """
    # Tiny three-page wiki: two people, one project
    (wiki_home / "people").mkdir(parents=True, exist_ok=True)
    (wiki_home / "projects").mkdir(parents=True, exist_ok=True)

    (wiki_home / "people" / "amanda-peffer.md").write_text(
        "---\nemails: [amanda@example.com]\n---\n\n"
        "# Amanda Peffer\n\n"
        "Amanda runs the Blade & Rose US retail partnership. "
        "She requested theme feedback on April 5.\n"
    )
    (wiki_home / "people" / "jon-sturos.md").write_text(
        "---\nphones: [+15551234567]\n---\n\n"
        "# Jon Sturos\n\n"
        "Jon owns Casita Roof and is quoting the July replacement.\n"
    )
    (wiki_home / "projects" / "blade-and-rose.md").write_text(
        "# Blade & Rose\n\n"
        "Shipping the new US Shopify theme with Amanda Peffer. "
        "Preview went live April 5; waiting on her feedback.\n"
    )

    # Drop a .qmd-config so qmd knows which collection to write into —
    # some qmd setups use per-dir config, others use CLI flags. We pass
    # -c explicitly to be safe.
    env = os.environ.copy()
    env["QMD_CACHE_DIR"] = str(qmd_cache)

    # Index the temp wiki under the canonical collection name
    subprocess.run(
        ["qmd", "update", "-c", QMD_COLLECTION],
        cwd=str(wiki_home),
        env=env,
        capture_output=True,
        timeout=30,
        check=False,
    )
    # Generate embeddings so vector queries return results. Without
    # embed the bm25 path still works, which is what we actually test.
    subprocess.run(
        ["qmd", "embed", "-c", QMD_COLLECTION],
        cwd=str(wiki_home),
        env=env,
        capture_output=True,
        timeout=120,
        check=False,
    )


def test_qmd_collection_matches_config_constant(isolated_home):
    """The ``QMD_COLLECTION`` constant must match a real collection.

    This is the canary that fails the instant someone renames the
    collection in code without also rebuilding the index, or vice
    versa. It doesn't require the local real wiki — it seeds its own.
    """
    home, wiki = isolated_home
    qmd_cache = home / "qmd_cache"
    qmd_cache.mkdir()
    _seed_wiki_and_index(wiki, qmd_cache)

    env = os.environ.copy()
    env["QMD_CACHE_DIR"] = str(qmd_cache)

    # Query directly against the constant name — no hardcoded string.
    result = subprocess.run(
        ["qmd", "search", "amanda", "-c", QMD_COLLECTION],
        cwd=str(wiki),
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )

    # ``qmd search`` prints results to stdout when the collection
    # exists and has content. If the collection is missing or empty,
    # qmd prints "Collection not found" or returns empty. Either way,
    # the raw stdout should mention amanda.
    assert "amanda" in result.stdout.lower(), (
        f"QMD collection {QMD_COLLECTION!r} returned no hits for a page "
        f"that definitely exists. Either the collection name drifted "
        f"(wiki_retriever was using a phantom name for months before we "
        f"caught it) or indexing failed silently. stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )


def test_wiki_retriever_build_context_returns_real_pages(isolated_home, monkeypatch):
    """``wiki_retriever.build_analysis_context`` must return page bodies.

    The integrate cycle's retrieval path has to deliver actual page
    content to the prompt — not just the index. If this test ever
    returns only the index catalog without any retrieved page body,
    something in the BM25 or vector path regressed.
    """
    home, wiki = isolated_home
    qmd_cache = home / "qmd_cache"
    qmd_cache.mkdir()
    _seed_wiki_and_index(wiki, qmd_cache)

    # Point QMD at the seeded cache for this test
    monkeypatch.setenv("QMD_CACHE_DIR", str(qmd_cache))

    from deja import wiki_retriever

    # Single signal mentioning amanda — should retrieve her page
    signals = [
        {
            "source": "imessage",
            "sender": "Alice",
            "text": "Just saw amanda peffer's email about the blade and rose theme",
            "timestamp": "2026-04-11T12:00:00Z",
        }
    ]

    context = wiki_retriever.build_analysis_context(signals)

    # Context should include the retrieved-pages section, not just
    # the catalog. We check for a substring from Amanda's page body
    # that only appears in the retrieved block, not the catalog.
    assert "Blade & Rose US retail partnership" in context or \
           "requested theme feedback" in context, (
        f"wiki_retriever.build_analysis_context returned context that "
        f"doesn't contain any body text from the retrieved pages. "
        f"Either retrieval is broken, the collection name is wrong "
        f"again, or the pages weren't indexed. Context length: "
        f"{len(context)}. First 800 chars: {context[:800]!r}"
    )


def test_qmd_query_wrapper_respects_collection_constant():
    """``mcp_server._qmd_query`` must use the ``QMD_COLLECTION`` constant.

    Not an integration test — a static guard against someone adding
    a new caller that hardcodes ``"wiki"`` or ``"Deja"`` again. We
    grep the source for bare string literals in query positions.
    """
    import pathlib
    import re

    src_root = pathlib.Path(__file__).parent.parent / "src" / "deja"
    offenders: list[tuple[str, int, str]] = []

    # Look for ``collection="wiki"`` or similar — the regression we
    # want to prevent. Legitimate uses go through QMD_COLLECTION.
    pattern = re.compile(r'collection\s*=\s*["\']([^"\']+)["\']')

    for py_file in src_root.rglob("*.py"):
        if "/tests/" in str(py_file):
            continue
        try:
            text = py_file.read_text(encoding="utf-8")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), start=1):
            m = pattern.search(line)
            if m and m.group(1) != "{}":  # skip f-string placeholders
                offenders.append((str(py_file.relative_to(src_root.parent.parent)), i, line.strip()))

    assert not offenders, (
        f"Hardcoded collection strings found outside QMD_COLLECTION constant:\n"
        + "\n".join(f"  {f}:{ln}: {text}" for f, ln, text in offenders)
        + "\n\nAll collection= kwargs must use the QMD_COLLECTION constant "
        "from deja.config, not a string literal. This is the guard that "
        "would have caught the wiki_retriever 'wiki' vs 'Deja' drift in "
        "April 2026 before it shipped."
    )
