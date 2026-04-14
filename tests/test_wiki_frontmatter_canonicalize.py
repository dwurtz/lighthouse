"""Tests for YAML frontmatter canonicalization.

Integrate occasionally produces one-line frontmatter where all keys
land between the opening and closing --- without newlines:

    ---date: 2026-04-06time: "17:47"people: [david-wurtz]projects: [foo]---

30+ event files had this shape before canonicalize_frontmatter was
added. These tests pin the repair behavior so future refactors don't
silently stop handling the corruption pattern.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from deja.wiki import canonicalize_frontmatter, extract_frontmatter


def test_canonicalize_one_line_event_frontmatter():
    """The exact shape integrate produces is repaired to multi-line."""
    corrupted = (
        '---date: 2026-04-06time: "17:47"people: [david-wurtz]'
        "projects: [defensive-drivers-institute-course]---\n"
        "# David studies defensive driving course\n"
    )
    repaired, was_repaired = canonicalize_frontmatter(corrupted)
    assert was_repaired is True
    assert "---\ndate: 2026-04-06\n" in repaired
    assert 'time: "17:47"' in repaired
    assert "people: [david-wurtz]" in repaired
    assert "projects: [defensive-drivers-institute-course]" in repaired
    assert repaired.endswith("# David studies defensive driving course\n")


def test_canonicalize_preserves_clean_multiline():
    """Clean multi-line frontmatter is returned unchanged."""
    clean = (
        "---\n"
        "date: 2026-04-06\n"
        "time: \"17:47\"\n"
        "people: [david-wurtz]\n"
        "projects: [foo]\n"
        "---\n"
        "# Body\n"
    )
    repaired, was_repaired = canonicalize_frontmatter(clean)
    assert was_repaired is False
    assert repaired == clean


def test_canonicalize_leaves_content_without_frontmatter_alone():
    """Content with no frontmatter shouldn't get a stub added."""
    content = "# Just a heading\nSome prose.\n"
    repaired, was_repaired = canonicalize_frontmatter(content)
    assert was_repaired is False
    assert repaired == content


def test_canonicalized_output_is_parseable_by_extract_frontmatter():
    """After repair, extract_frontmatter() works on the result.

    This is the integration contract — canonicalize + extract must
    produce frontmatter the rest of the codebase can parse.
    """
    import yaml

    corrupted = (
        "---date: 2026-04-06people: [david-wurtz, sara-niedzialek]"
        "projects: []---\nBody here.\n"
    )
    repaired, _ = canonicalize_frontmatter(corrupted)
    fm_block, body = extract_frontmatter(repaired)
    assert fm_block  # non-empty
    # Strip --- lines and parse as yaml
    inner = "\n".join(
        line for line in fm_block.split("\n") if not line.strip() == "---"
    )
    data = yaml.safe_load(inner)
    assert data["date"] == "2026-04-06" or str(data["date"]).startswith("2026-04-06")
    assert data["people"] == ["david-wurtz", "sara-niedzialek"]
    assert data["projects"] == []
    assert "Body here." in body


def test_canonicalize_handles_quoted_values_with_colons():
    """Quoted strings that contain colons (like times) aren't split."""
    corrupted = '---date: 2026-04-06time: "17:47:30"people: []projects: []---\nBody.\n'
    repaired, was_repaired = canonicalize_frontmatter(corrupted)
    assert was_repaired is True
    # The colon inside the quoted value must survive
    assert 'time: "17:47:30"' in repaired
