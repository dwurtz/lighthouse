"""wiki_index.rebuild_index: hygiene rules for the LLM-facing catalog.

The index is injected into every analysis cycle AND every vision call, so
noise here is expensive — placeholder stubs and unbounded growth directly
hurt grounding quality. These tests lock the hygiene contract.
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture
def wiki_mod_with_patched_paths(isolated_home, monkeypatch):
    """wiki_index captures WIKI_DIR and INDEX_PATH at import time — patch both."""
    _, wiki = isolated_home
    import deja.wiki_catalog as wi
    monkeypatch.setattr(wi, "WIKI_DIR", wiki)
    monkeypatch.setattr(wi, "INDEX_PATH", wiki / "index.md")
    (wiki / "people").mkdir()
    (wiki / "projects").mkdir()
    return wi, wiki


def _write_page(wiki, category, slug, title, body):
    p = wiki / category / f"{slug}.md"
    p.write_text(f"# {title}\n\n{body}\n")
    return p


def test_rebuild_strips_placeholder_summaries_keeps_slugs(wiki_mod_with_patched_paths):
    """Placeholder summaries (`---`, `TBD`, empty) should be stripped but the
    slug itself must still appear in the index — the slug is the grounding
    signal for the LLM, the placeholder text is the noise."""
    wi, wiki = wiki_mod_with_patched_paths
    _write_page(wiki, "people", "alice", "Alice", "Alice is David's dentist.")
    _write_page(wiki, "people", "bob", "Bob", "---")
    _write_page(wiki, "people", "carol", "Carol", "")  # empty body
    _write_page(wiki, "people", "dave", "Dave", "TBD")

    count = wi.rebuild_index()
    assert count == 4

    text = (wiki / "index.md").read_text()
    # All four slugs present for grounding
    assert "[[alice]]" in text
    assert "[[bob]]" in text
    assert "[[carol]]" in text
    assert "[[dave]]" in text
    # Alice has a real summary; the others are bare
    assert "[[alice]] — Alice is David's dentist." in text
    assert "[[bob]]\n" in text
    # No noisy placeholder dashes ever rendered as a summary
    assert " — ---" not in text
    assert " — TBD" not in text


def test_rebuild_emits_real_summary(wiki_mod_with_patched_paths):
    wi, wiki = wiki_mod_with_patched_paths
    _write_page(wiki, "projects", "palo-alto", "Palo Alto Relocation",
                "Moving the family from Phoenix to Palo Alto in July.")
    wi.rebuild_index()
    text = (wiki / "index.md").read_text()
    assert "- [[palo-alto]] — Moving the family" in text


def test_rebuild_respects_max_entries_cap(wiki_mod_with_patched_paths, monkeypatch):
    wi, wiki = wiki_mod_with_patched_paths
    monkeypatch.setattr(wi, "_MAX_ENTRIES", 5)

    # Create 10 pages with staggered mtimes so we can verify the cap keeps
    # the freshest ones.
    for i in range(10):
        p = _write_page(
            wiki, "projects", f"proj-{i:02d}", f"Project {i}",
            f"Description for project {i}.",
        )
        os.utime(p, (1_000_000 + i, 1_000_000 + i))

    count = wi.rebuild_index()
    assert count == 5

    text = (wiki / "index.md").read_text()
    # Freshest 5 (proj-05..proj-09) should be present; older should be gone
    assert "proj-09" in text
    assert "proj-05" in text
    assert "proj-04" not in text
    assert "proj-00" not in text


def test_rebuild_empty_wiki_writes_placeholder(wiki_mod_with_patched_paths):
    wi, wiki = wiki_mod_with_patched_paths
    count = wi.rebuild_index()
    assert count == 0
    text = (wiki / "index.md").read_text()
    assert "No pages yet" in text


def test_rebuild_all_placeholders_still_indexed_as_bare_slugs(wiki_mod_with_patched_paths):
    """Pages that only have placeholder summaries should still appear in the
    index as bare slugs — they're valid wiki entities, we just don't have
    anything informative to say about them yet."""
    wi, wiki = wiki_mod_with_patched_paths
    _write_page(wiki, "people", "alice", "Alice", "---")
    _write_page(wiki, "people", "bob", "Bob", "---")
    count = wi.rebuild_index()
    assert count == 2
    text = (wiki / "index.md").read_text()
    assert "[[alice]]" in text
    assert "[[bob]]" in text
    assert " — ---" not in text


def test_rebuild_skips_yaml_frontmatter(wiki_mod_with_patched_paths):
    """Pages starting with a YAML frontmatter block should still surface
    their real first-paragraph summary — the frontmatter delimiter must
    not be mistaken for a placeholder."""
    wi, wiki = wiki_mod_with_patched_paths
    page = wiki / "people" / "adam.md"
    page.write_text(
        "---\n"
        "keywords: [anthropic, recruiting]\n"
        "alias: [feldman]\n"
        "---\n"
        "# Adam Feldman\n"
        "\n"
        "Adam Feldman is someone David interviewed with at Anthropic.\n"
    )
    wi.rebuild_index()
    text = (wiki / "index.md").read_text()
    assert "[[adam]] — Adam Feldman is someone David interviewed with at Anthropic." in text


def test_rebuild_handles_unterminated_frontmatter(wiki_mod_with_patched_paths):
    """Malformed frontmatter shouldn't crash or silently drop the page."""
    wi, wiki = wiki_mod_with_patched_paths
    page = wiki / "people" / "bob.md"
    page.write_text("---\nkeywords: [foo]\n# Bob\n\nReal content here.\n")
    count = wi.rebuild_index()
    assert count == 1  # page is still indexed, even if extraction is degraded


def test_is_placeholder_summary_variants():
    from deja.wiki_catalog import _is_placeholder_summary
    assert _is_placeholder_summary("---")
    assert _is_placeholder_summary("--")
    assert _is_placeholder_summary("")
    assert _is_placeholder_summary("   ")
    assert _is_placeholder_summary("TBD")
    assert _is_placeholder_summary("todo")
    assert not _is_placeholder_summary("Alice is David's dentist.")
    assert not _is_placeholder_summary("A real sentence.")
