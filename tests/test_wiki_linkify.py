"""Deterministic wiki linkifier.

These tests lock the behavior a nightly linkify pass must maintain:
- linkifies unlinked mentions of known entities
- first-occurrence only per slug per page
- preserves YAML frontmatter verbatim
- never touches code blocks, inline code, existing wiki links, markdown links
- skips self-reference
- excludes the user's self-page from the catalog
- aliases from frontmatter are honored
- longest phrase wins on overlapping matches
- broken-link detection finds missing targets
"""

from __future__ import annotations

import pytest


@pytest.fixture
def linkify_mod(isolated_home, monkeypatch):
    """wiki_linkify captured WIKI_DIR at import time — patch it."""
    _, wiki = isolated_home
    import lighthouse.wiki_linkify as wl
    monkeypatch.setattr(wl, "WIKI_DIR", wiki)
    (wiki / "people").mkdir()
    (wiki / "projects").mkdir()
    return wl, wiki


def _page(wiki, category, slug, content):
    p = wiki / category / f"{slug}.md"
    p.write_text(content)
    return p


# ---------------------------------------------------------------------------
# build_catalog
# ---------------------------------------------------------------------------

def test_build_catalog_extracts_title_slug_and_aliases(linkify_mod):
    wl, wiki = linkify_mod
    _page(wiki, "people", "jane-doe",
        "---\naliases: [Jane, Janey, JD]\n---\n# Jane Doe\n\nA person.\n"
    )
    _page(wiki, "projects", "palo-alto-relocation",
        "# Palo Alto Relocation\n\nMoving.\n"
    )
    catalog = wl.build_catalog(wiki)
    assert len(catalog) == 2
    jane = next(e for e in catalog if e.slug == "jane-doe")
    assert "Jane Doe" in jane.phrases
    assert "Jane" in jane.phrases
    assert "Janey" in jane.phrases
    # "JD" is 2 chars — below _MIN_MATCH_LEN, deliberately dropped
    assert "JD" not in jane.phrases
    palo = next(e for e in catalog if e.slug == "palo-alto-relocation")
    assert palo.title == "Palo Alto Relocation"


def test_build_catalog_excludes_self_page(linkify_mod):
    wl, wiki = linkify_mod
    _page(wiki, "people", "me",
        "---\nself: true\naliases: [Me]\n---\n# Me\n\nSelf.\n"
    )
    _page(wiki, "people", "other", "# Other\n\nA person.\n")
    catalog = wl.build_catalog(wiki)
    slugs = {e.slug for e in catalog}
    assert "me" not in slugs
    assert "other" in slugs


def test_build_catalog_drops_short_phrases(linkify_mod):
    """Phrases shorter than _MIN_MATCH_LEN are too noisy to auto-link."""
    wl, wiki = linkify_mod
    _page(wiki, "people", "ai", "# AI\n\nInitials.\n")
    _page(wiki, "people", "longer-name", "# Longer Name\n\nA person.\n")
    catalog = wl.build_catalog(wiki)
    # "AI" and "ai" are both length 2, below the threshold — entity has no
    # phrases and should be excluded
    slugs = {e.slug for e in catalog}
    assert "ai" not in slugs
    assert "longer-name" in slugs


# ---------------------------------------------------------------------------
# linkify_body — core behavior
# ---------------------------------------------------------------------------

def _cat(**kw):
    from lighthouse.wiki_linkify import Entity
    phrases = tuple(kw.pop("phrases", (kw.get("title", kw["slug"]),)))
    return Entity(
        slug=kw["slug"],
        category=kw.get("category", "people"),
        title=kw.get("title", kw["slug"]),
        phrases=phrases,
    )


def test_linkify_basic_match():
    from lighthouse.wiki_linkify import linkify_body
    catalog = [_cat(slug="jane-doe", title="Jane Doe", phrases=("Jane Doe", "Jane"))]
    body = "I talked to Jane Doe about the project."
    out, added = linkify_body(body, catalog)
    assert "[[jane-doe|Jane Doe]]" in out
    assert added == {"jane-doe": 1}


def test_linkify_first_occurrence_only():
    from lighthouse.wiki_linkify import linkify_body
    catalog = [_cat(slug="jane-doe", title="Jane Doe", phrases=("Jane Doe",))]
    body = "Jane Doe called. Later Jane Doe emailed. Finally Jane Doe texted."
    out, added = linkify_body(body, catalog)
    # Only the first occurrence is linked
    assert out.count("[[jane-doe") == 1
    assert out.count("Jane Doe") >= 2  # subsequent plain mentions remain
    assert added == {"jane-doe": 1}


def test_linkify_skips_self_reference():
    from lighthouse.wiki_linkify import linkify_body
    catalog = [_cat(slug="jane-doe", title="Jane Doe", phrases=("Jane Doe",))]
    body = "Jane Doe writes about herself."
    out, _ = linkify_body(body, catalog, self_slug="jane-doe")
    assert "[[" not in out


def test_linkify_skips_existing_wiki_link():
    from lighthouse.wiki_linkify import linkify_body
    catalog = [_cat(slug="jane-doe", title="Jane Doe", phrases=("Jane Doe",))]
    body = "Already linked: [[jane-doe|Jane Doe]] is great."
    out, added = linkify_body(body, catalog)
    assert out == body
    assert added == {}


def test_linkify_respects_existing_title_form_link():
    """An LLM-written [[Soccer Carpool]] (title form) should suppress a
    later [[soccer-carpool|soccer carpool]] on the same page — same
    entity, no duplicate linking."""
    from lighthouse.wiki_linkify import linkify_body
    catalog = [_cat(slug="soccer-carpool", title="Soccer Carpool",
                    phrases=("Soccer Carpool", "soccer carpool"))]
    body = (
        "Part of the [[Soccer Carpool]] group. "
        "David is also reviewing the soccer carpool schedule later."
    )
    out, added = linkify_body(body, catalog)
    # Output unchanged — the entity is already linked (title-form)
    assert out == body
    assert added == {}


def test_linkify_skips_fenced_code_block():
    from lighthouse.wiki_linkify import linkify_body
    catalog = [_cat(slug="palo-alto", title="Palo Alto", phrases=("Palo Alto",))]
    body = "```\nPalo Alto\n```\n\nBut Palo Alto matters."
    out, added = linkify_body(body, catalog)
    # The code block occurrence is untouched
    assert "```\nPalo Alto\n```" in out
    # The prose occurrence IS linked
    assert "[[palo-alto|Palo Alto]]" in out
    assert added == {"palo-alto": 1}


def test_linkify_skips_inline_code():
    from lighthouse.wiki_linkify import linkify_body
    catalog = [_cat(slug="loop-py", title="loop.py", phrases=("loop.py",))]
    body = "Check `loop.py` for the bug."
    out, added = linkify_body(body, catalog)
    # Inline-code occurrence is skipped; no other mention exists → no link
    assert out == body
    assert added == {}


def test_linkify_skips_markdown_link():
    from lighthouse.wiki_linkify import linkify_body
    catalog = [_cat(slug="jane-doe", title="Jane Doe", phrases=("Jane Doe",))]
    body = "See [Jane Doe](https://example.com/janedoe)."
    out, added = linkify_body(body, catalog)
    assert out == body
    assert added == {}


def test_linkify_word_boundary_non_word_chars():
    """Phrases containing & or ' must still bound correctly."""
    from lighthouse.wiki_linkify import linkify_body
    catalog = [_cat(slug="blade-and-rose", title="Blade & Rose", phrases=("Blade & Rose",))]
    body = "The Blade & Rose brand is great."
    out, _ = linkify_body(body, catalog)
    assert "[[blade-and-rose|Blade & Rose]]" in out


def test_linkify_respects_word_boundaries():
    """'rose' should not match inside 'rosemary'."""
    from lighthouse.wiki_linkify import linkify_body
    catalog = [_cat(slug="rose", title="Rose", phrases=("Rose",))]
    body = "I planted rosemary and basil."
    out, added = linkify_body(body, catalog)
    assert out == body
    assert added == {}


def test_linkify_longest_phrase_wins():
    """Overlapping matches should resolve to the most specific entity."""
    from lighthouse.wiki_linkify import linkify_body
    catalog = [
        _cat(slug="rose", title="Rose", phrases=("Rose",)),
        _cat(slug="blade-and-rose", title="Blade & Rose", phrases=("Blade & Rose",)),
    ]
    body = "I bought from Blade & Rose yesterday."
    out, added = linkify_body(body, catalog)
    # The longer phrase won; rose is not separately linked
    assert "[[blade-and-rose|Blade & Rose]]" in out
    assert "rose" not in added
    assert added == {"blade-and-rose": 1}


def test_linkify_case_insensitive_match_preserves_case():
    from lighthouse.wiki_linkify import linkify_body
    catalog = [_cat(slug="jane-doe", title="Jane Doe", phrases=("Jane Doe",))]
    body = "I talked to jane doe yesterday."
    out, _ = linkify_body(body, catalog)
    # Matched text ("jane doe") is preserved in the display alias
    assert "[[jane-doe|jane doe]]" in out


def test_linkify_bare_form_when_matched_equals_slug():
    from lighthouse.wiki_linkify import linkify_body
    catalog = [_cat(slug="palo-alto-relocation", phrases=("palo-alto-relocation",))]
    body = "The palo-alto-relocation project is ongoing."
    out, _ = linkify_body(body, catalog)
    # No display alias since matched text equals slug exactly
    assert "[[palo-alto-relocation]]" in out
    assert "[[palo-alto-relocation|palo-alto-relocation]]" not in out


# ---------------------------------------------------------------------------
# linkify_wiki — full pass + idempotency + frontmatter preservation
# ---------------------------------------------------------------------------

def test_linkify_wiki_preserves_frontmatter(linkify_mod):
    wl, wiki = linkify_mod
    _page(wiki, "people", "jane-doe", "# Jane Doe\n\nA person.\n")
    _page(wiki, "projects", "acme",
        "---\naliases: [Acme Corp]\n---\n# Acme\n\nJane Doe works at Acme.\n"
    )
    wl.linkify_wiki()
    acme_text = (wiki / "projects" / "acme.md").read_text()
    # Frontmatter is intact
    assert acme_text.startswith("---\naliases: [Acme Corp]\n---\n")
    # Body has the link
    assert "[[jane-doe|Jane Doe]]" in acme_text


def test_linkify_wiki_is_idempotent(linkify_mod):
    wl, wiki = linkify_mod
    _page(wiki, "people", "jane-doe", "# Jane Doe\n\nA person.\n")
    _page(wiki, "projects", "acme", "# Acme\n\nJane Doe runs it.\n")

    report1 = wl.linkify_wiki()
    assert report1.links_added == 1
    report2 = wl.linkify_wiki()
    # Second pass sees the links already present and makes no changes
    assert report2.links_added == 0
    assert report2.pages_changed == 0


def test_linkify_wiki_reports_counts(linkify_mod):
    wl, wiki = linkify_mod
    _page(wiki, "people", "jane-doe", "# Jane Doe\n\nHi.\n")
    _page(wiki, "projects", "acme", "# Acme\n\nJane Doe runs it.\n")
    _page(wiki, "projects", "beta", "# Beta\n\nAlso Jane Doe.\n")

    report = wl.linkify_wiki()
    assert report.pages_scanned == 3
    assert report.pages_changed == 2  # acme + beta, not jane-doe itself
    assert report.links_added == 2
    assert report.links_by_slug == {"jane-doe": 2}


def test_linkify_wiki_dry_run(linkify_mod):
    wl, wiki = linkify_mod
    _page(wiki, "people", "jane-doe", "# Jane Doe\n\nHi.\n")
    _page(wiki, "projects", "acme", "# Acme\n\nJane Doe runs it.\n")

    report = wl.linkify_wiki(dry_run=True)
    assert report.pages_changed == 1
    # File on disk is unchanged
    assert "[[" not in (wiki / "projects" / "acme.md").read_text()


# ---------------------------------------------------------------------------
# Broken-link detection
# ---------------------------------------------------------------------------

def test_find_broken_refs_flags_missing_target(linkify_mod):
    wl, wiki = linkify_mod
    _page(wiki, "projects", "acme",
        "# Acme\n\nSee [[jane-doe]] and [[nonexistent-person]].\n"
    )
    _page(wiki, "people", "jane-doe", "# Jane Doe\n\nReal.\n")

    broken = wl.find_broken_refs(wiki)
    assert ("projects/acme", "nonexistent-person") in broken
    assert not any(target == "jane-doe" for _, target in broken)


def test_find_broken_refs_resolves_title_style_links(linkify_mod):
    """Obsidian-style `[[Ship New Blade & Rose Theme]]` with title + punct
    should resolve to a page whose slug is `ship-new-blade-rose-theme`."""
    wl, wiki = linkify_mod
    _page(wiki, "projects", "ship-new-blade-rose-theme",
        "# Ship New Blade & Rose Theme\n\nA Shopify theme.\n"
    )
    _page(wiki, "projects", "other",
        "# Other\n\nSee [[Ship New Blade & Rose Theme]].\n"
    )
    broken = wl.find_broken_refs(wiki)
    assert not broken


def test_find_broken_refs_resolves_title_with_quotes_and_parens(linkify_mod):
    wl, wiki = linkify_mod
    _page(wiki, "people", "justin-molly-s-dad",
        "# Justin (Molly's Dad)\n\nA person.\n"
    )
    _page(wiki, "projects", "other",
        "# Other\n\nTalked to [[Justin (Molly's Dad)]].\n"
    )
    broken = wl.find_broken_refs(wiki)
    assert not broken


def test_find_broken_refs_ignores_self_page_as_target(linkify_mod):
    """Links to the self-page should resolve even though it's not in the
    catalog (self-page is excluded from auto-linking but is a valid target)."""
    wl, wiki = linkify_mod
    _page(wiki, "people", "me",
        "---\nself: true\n---\n# Me\n\nSelf.\n"
    )
    _page(wiki, "projects", "acme",
        "# Acme\n\nSee [[me]] about this.\n"
    )
    broken = wl.find_broken_refs(wiki)
    assert not any(target == "me" for _, target in broken)
