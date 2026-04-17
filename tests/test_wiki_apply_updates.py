"""Structural splicing tests for wiki.apply_updates.

Pins the 2026-04-16 change that removes integrate's authority over
people/project frontmatter. These tests exist to prevent regression of
the recurring "integrate clobbered inner_circle / phones / emails" bug.

Three writer domains of wiki frontmatter:
  1. people_enrichment.py owns emails / phones / aliases
  2. onboarding writes self / preferred_name once at creation
  3. the user owns inner_circle via manual edit

Integrate writes body prose only. The write path splices its
``body_markdown`` onto whatever YAML already exists, so integrate
cannot structurally drop a user- or enrichment-owned field.

Events are different — integrate owns their frontmatter, supplied via
the ``event_metadata`` structured field and serialized by the write
path.
"""

from __future__ import annotations

import yaml

from deja import wiki as wiki_mod


def _stub_side_effects(monkeypatch):
    monkeypatch.setattr("deja.wiki_git.ensure_repo", lambda: None)
    monkeypatch.setattr("deja.wiki_git.commit_changes", lambda msg: None)
    monkeypatch.setattr("deja.wiki_catalog.rebuild_index", lambda: None)
    import deja.llm.search as search
    monkeypatch.setattr(search, "refresh_index", lambda: None)


# ---------------------------------------------------------------------------
# Person page — frontmatter preserved verbatim, body replaced
# ---------------------------------------------------------------------------


def test_update_person_preserves_inner_circle_phones_emails(isolated_home, monkeypatch):
    """The canonical regression test: Dominique-style page keeps its flags."""
    _stub_side_effects(monkeypatch)

    people_dir = wiki_mod.WIKI_DIR / "people"
    people_dir.mkdir(parents=True, exist_ok=True)
    page = people_dir / "dominique.md"
    page.write_text(
        "---\n"
        "preferred_name: Dominique\n"
        "emails: [dominique@example.com]\n"
        "phones: [\"+1-555-0100\"]\n"
        "aliases: [Dom]\n"
        "inner_circle: true\n"
        "---\n"
        "# Dominique\n\n"
        "Old body that will be replaced.\n"
    )

    applied = wiki_mod.apply_updates([{
        "category": "people",
        "slug": "dominique",
        "action": "update",
        "body_markdown": "# Dominique\n\nNew prose about Dominique.\n",
        "reason": "T1 signal from Dominique",
    }])

    assert applied == 1
    text = page.read_text()
    fm, body = wiki_mod.extract_frontmatter(text)
    assert fm, "frontmatter must still be present"

    data = yaml.safe_load(fm.strip().strip("-").strip())
    # Every hand-curated key survived.
    assert data["preferred_name"] == "Dominique"
    assert data["emails"] == ["dominique@example.com"]
    assert data["phones"] == ["+1-555-0100"]
    assert data["aliases"] == ["Dom"]
    assert data["inner_circle"] is True

    # Body was replaced.
    assert "New prose about Dominique." in body
    assert "Old body that will be replaced." not in body


def test_body_only_payload_leaves_rich_frontmatter_untouched(isolated_home, monkeypatch):
    """A body-only update is the steady-state integrate shape."""
    _stub_side_effects(monkeypatch)

    people_dir = wiki_mod.WIKI_DIR / "people"
    people_dir.mkdir(parents=True, exist_ok=True)
    page = people_dir / "kim.md"
    original_fm = (
        "---\n"
        "preferred_name: Kim\n"
        "emails: [kim@example.com, kim.lee@work.test]\n"
        "phones: [\"+1-555-0199\"]\n"
        "aliases: [Kimmy, K]\n"
        "inner_circle: true\n"
        "self: false\n"
        "---\n"
    )
    page.write_text(original_fm + "# Kim\n\nOriginal body.\n")

    wiki_mod.apply_updates([{
        "category": "people",
        "slug": "kim",
        "action": "update",
        "body_markdown": "# Kim\n\nKim confirmed no carpool drop-off was needed.\n",
        "reason": "kim's T1 message",
    }])

    text = page.read_text()
    # Entire original frontmatter block survived (bytewise up to yaml
    # round-trip — compare keys via parse).
    fm, body = wiki_mod.extract_frontmatter(text)
    data = yaml.safe_load(
        "\n".join(
            ln for ln in fm.splitlines() if ln.strip() != "---"
        )
    )
    assert data["preferred_name"] == "Kim"
    assert data["emails"] == ["kim@example.com", "kim.lee@work.test"]
    assert data["phones"] == ["+1-555-0199"]
    assert data["aliases"] == ["Kimmy", "K"]
    assert data["inner_circle"] is True
    assert data["self"] is False
    assert "Kim confirmed" in body


def test_body_markdown_with_leading_yaml_block_is_stripped(isolated_home, monkeypatch):
    """Defensive: if the model slips a ---YAML--- block back in, strip it.

    The structural guard must not trust prompt discipline alone.
    """
    _stub_side_effects(monkeypatch)

    people_dir = wiki_mod.WIKI_DIR / "people"
    people_dir.mkdir(parents=True, exist_ok=True)
    page = people_dir / "sam.md"
    page.write_text(
        "---\n"
        "preferred_name: Sam\n"
        "inner_circle: true\n"
        "---\n"
        "# Sam\n\nOld.\n"
    )

    # The model slipped a (wrong, missing keys) YAML block into body.
    slippage = (
        "---\n"
        "preferred_name: Sam\n"  # inner_circle DROPPED — classic regression
        "---\n"
        "# Sam\n\nNew body.\n"
    )
    wiki_mod.apply_updates([{
        "category": "people",
        "slug": "sam",
        "action": "update",
        "body_markdown": slippage,
        "reason": "T1",
    }])

    text = page.read_text()
    fm, body = wiki_mod.extract_frontmatter(text)
    data = yaml.safe_load(
        "\n".join(ln for ln in fm.splitlines() if ln.strip() != "---")
    )
    # The existing inner_circle: true survived — because the model's
    # slipped YAML was stripped and the original fm was spliced back.
    assert data["inner_circle"] is True
    assert data["preferred_name"] == "Sam"
    assert "New body." in body
    # Critical: the slipped YAML didn't leak into the body either.
    assert "preferred_name: Sam\n---" not in body


# ---------------------------------------------------------------------------
# Event pages — frontmatter OWNED by integrate via event_metadata
# ---------------------------------------------------------------------------


def test_event_update_produces_quoted_time(isolated_home, monkeypatch):
    """time: is always double-quoted. The rest of the code depends on it."""
    _stub_side_effects(monkeypatch)

    applied = wiki_mod.apply_updates([{
        "category": "events",
        "slug": "2026-04-16/carpool-plan",
        "action": "update",
        "body_markdown": "# Carpool plan\n\n[[kim]] confirmed the route.\n",
        "event_metadata": {
            "date": "2026-04-16",
            "time": "14:30",
            "people": ["kim", "david-wurtz"],
            "projects": ["school-carpool"],
        },
        "reason": "T1 message from kim",
    }])
    assert applied == 1

    path = wiki_mod.WIKI_DIR / "events" / "2026-04-16" / "carpool-plan.md"
    text = path.read_text()
    assert 'time: "14:30"' in text
    # Lists are flat inline.
    assert "people: [kim, david-wurtz]" in text
    assert "projects: [school-carpool]" in text
    # Body is present.
    assert "Carpool plan" in text


def test_event_empty_time_is_quoted_empty_string(isolated_home, monkeypatch):
    """Empty time still renders as `time: ""` so downstream parsers don't trip."""
    _stub_side_effects(monkeypatch)

    wiki_mod.apply_updates([{
        "category": "events",
        "slug": "2026-04-16/ambient-observation",
        "action": "update",
        "body_markdown": "# Ambient\n\nSaw a quiet afternoon.\n",
        "event_metadata": {
            "date": "2026-04-16",
            "time": "",
            "people": ["self"],
            "projects": [],
        },
        "reason": "T3",
    }])

    path = wiki_mod.WIKI_DIR / "events" / "2026-04-16" / "ambient-observation.md"
    text = path.read_text()
    assert 'time: ""' in text
    assert "people: [self]" in text
    assert "projects: []" in text


def test_event_missing_metadata_is_skipped(isolated_home, monkeypatch):
    """If event_metadata is missing we refuse to write — surfacing the bug
    is better than silently writing a broken event page."""
    _stub_side_effects(monkeypatch)

    applied = wiki_mod.apply_updates([{
        "category": "events",
        "slug": "2026-04-16/broken-event",
        "action": "update",
        "body_markdown": "# Broken\n",
        "reason": "missing metadata",
    }])
    assert applied == 0
    assert not (wiki_mod.WIKI_DIR / "events" / "2026-04-16" / "broken-event.md").exists()


# ---------------------------------------------------------------------------
# Creation path
# ---------------------------------------------------------------------------


def test_create_person_synthesizes_preferred_name(isolated_home, monkeypatch):
    """Fresh person page gets preferred_name from the slug."""
    _stub_side_effects(monkeypatch)

    applied = wiki_mod.apply_updates([{
        "category": "people",
        "slug": "marie-curie",
        "action": "create",
        "body_markdown": "# Marie Curie\n\nPhysicist.\n",
        "reason": "T1",
    }])
    assert applied == 1

    path = wiki_mod.WIKI_DIR / "people" / "marie-curie.md"
    text = path.read_text()
    fm, body = wiki_mod.extract_frontmatter(text)
    data = yaml.safe_load(
        "\n".join(ln for ln in fm.splitlines() if ln.strip() != "---")
    )
    assert data["preferred_name"] == "Marie Curie"
    assert "Physicist." in body


def test_create_project_has_empty_frontmatter_block(isolated_home, monkeypatch):
    """Projects have no structured frontmatter keys by default."""
    _stub_side_effects(monkeypatch)

    wiki_mod.apply_updates([{
        "category": "projects",
        "slug": "window-cleaner-search",
        "action": "create",
        "body_markdown": "# Window cleaner search\n\nLooking for a new one.\n",
        "reason": "T1",
    }])

    path = wiki_mod.WIKI_DIR / "projects" / "window-cleaner-search.md"
    text = path.read_text()
    # Page opens with a frontmatter block so the structural shape is uniform,
    # but the block is empty.
    assert text.startswith("---\n---\n") or text.startswith("---\n\n---\n")
    assert "Looking for a new one." in text


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


def test_delete_removes_file(isolated_home, monkeypatch):
    _stub_side_effects(monkeypatch)

    wiki_mod.write_page("projects", "ephemeral", "---\n---\n# Ephemeral\n")
    path = wiki_mod.WIKI_DIR / "projects" / "ephemeral.md"
    assert path.exists()

    applied = wiki_mod.apply_updates([{
        "category": "projects",
        "slug": "ephemeral",
        "action": "delete",
        "reason": "user retracted",
    }])
    assert applied == 1
    assert not path.exists()


# ---------------------------------------------------------------------------
# Legacy `content` back-compat
# ---------------------------------------------------------------------------


def test_legacy_content_field_still_splices_onto_existing_frontmatter(
    isolated_home, monkeypatch,
):
    """Old bundled apps emit `content` (with or without leading YAML).
    apply_updates must still splice correctly and not clobber fm."""
    _stub_side_effects(monkeypatch)

    people_dir = wiki_mod.WIKI_DIR / "people"
    people_dir.mkdir(parents=True, exist_ok=True)
    page = people_dir / "alex.md"
    page.write_text(
        "---\n"
        "preferred_name: Alex\n"
        "inner_circle: true\n"
        "phones: [\"+1-555-0101\"]\n"
        "---\n"
        "# Alex\n\nOld.\n"
    )

    # Legacy shape: `content` with a (wrong, missing-keys) YAML header.
    wiki_mod.apply_updates([{
        "category": "people",
        "slug": "alex",
        "action": "update",
        "content": "---\npreferred_name: Alex\n---\n# Alex\n\nAlex replied today.\n",
        "reason": "legacy",
    }])

    text = page.read_text()
    fm, body = wiki_mod.extract_frontmatter(text)
    data = yaml.safe_load(
        "\n".join(ln for ln in fm.splitlines() if ln.strip() != "---")
    )
    # Critical: inner_circle and phones still present.
    assert data["inner_circle"] is True
    assert data["phones"] == ["+1-555-0101"]
    assert "Alex replied today." in body


def test_legacy_event_content_extracts_metadata(isolated_home, monkeypatch):
    """Legacy event `content` with a YAML block still produces a valid event."""
    _stub_side_effects(monkeypatch)

    legacy_content = (
        "---\n"
        "date: 2026-04-16\n"
        "time: \"09:15\"\n"
        "people: [sam]\n"
        "projects: []\n"
        "---\n"
        "# Sam checks in\n\nQuick hello.\n"
    )
    applied = wiki_mod.apply_updates([{
        "category": "events",
        "slug": "2026-04-16/sam-checks-in",
        "action": "update",
        "content": legacy_content,
        "reason": "legacy event",
    }])
    assert applied == 1

    path = wiki_mod.WIKI_DIR / "events" / "2026-04-16" / "sam-checks-in.md"
    text = path.read_text()
    assert 'time: "09:15"' in text
    assert "people: [sam]" in text
    assert "projects: []" in text
    assert "Quick hello." in text


# ---------------------------------------------------------------------------
# _strip_leading_frontmatter helper direct unit test
# ---------------------------------------------------------------------------


def test_strip_leading_frontmatter_helper():
    f = wiki_mod._strip_leading_frontmatter
    assert f("# Title\nprose") == "# Title\nprose"
    assert f("---\nkey: value\n---\n# Title\n") == "# Title\n"
    assert f("---\nself: true\nemails: [x@y]\n---\n# Title\n") == "# Title\n"
    # One-line corruption shape also handled.
    assert f('---date: 2026-04-16---\n# T\n') == "# T\n"
    assert f("") == ""
