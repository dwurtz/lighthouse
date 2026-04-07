"""UserProfile loading — the single source of identity for every prompt."""

from __future__ import annotations

import pytest

from deja.identity import UserProfile, _first_name, _parse_self_page, load_user


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def test_first_name_prefers_preferred():
    assert _first_name("Jane Roberta Doe", "Jane") == "Jane"


def test_first_name_falls_back_to_first_token():
    assert _first_name("Jane Roberta Doe", "") == "Jane"


def test_first_name_handles_empty_full():
    assert _first_name("", "") == ""


def test_parse_self_page_frontmatter_and_body():
    text = (
        "---\n"
        "self: true\n"
        "email: jane@example.com\n"
        "phone: '+14155551234'\n"
        "preferred_name: Jane\n"
        "---\n"
        "# Jane Doe\n"
        "\n"
        "Jane is a product manager at Acme.\n"
    )
    meta, title, body = _parse_self_page(text)
    assert meta["self"] is True
    assert meta["email"] == "jane@example.com"
    assert meta["phone"] == "+14155551234"
    assert meta["preferred_name"] == "Jane"
    assert title == "Jane Doe"
    assert body == "Jane is a product manager at Acme."


def test_parse_self_page_no_frontmatter():
    text = "# Solo\n\nNo frontmatter here.\n"
    meta, title, body = _parse_self_page(text)
    assert meta == {}
    assert title == "Solo"
    assert "No frontmatter here" in body


def test_parse_self_page_malformed_frontmatter_degrades_gracefully():
    text = "---\nnot: valid: yaml: [unclosed\n---\n# Title\n\nBody.\n"
    meta, title, body = _parse_self_page(text)
    # Bad YAML → meta is empty, but title/body still extract
    assert meta == {}
    assert title == "Title"
    assert body == "Body."


# ---------------------------------------------------------------------------
# load_user — happy path
# ---------------------------------------------------------------------------

def _write_self_page(wiki, slug, content):
    (wiki / "people").mkdir(exist_ok=True)
    (wiki / "people" / f"{slug}.md").write_text(content)


def test_load_user_from_frontmatter_flag(isolated_home, monkeypatch):
    _, wiki = isolated_home
    import deja.identity as user_mod
    monkeypatch.setattr(user_mod, "WIKI_DIR", wiki)

    _write_self_page(wiki, "jane-doe",
        "---\n"
        "self: true\n"
        "email: jane@example.com\n"
        "phone: '+14155551234'\n"
        "preferred_name: Jane\n"
        "---\n"
        "# Jane Doe\n"
        "\n"
        "Jane is a PM at Acme Corp.\n"
    )

    user = load_user()
    assert user.slug == "jane-doe"
    assert user.name == "Jane Doe"
    assert user.first_name == "Jane"
    assert user.email == "jane@example.com"
    assert user.phone == "+14155551234"
    assert "Acme" in user.profile_md
    assert not user.is_generic


def test_load_user_respects_config_user_slug(isolated_home, monkeypatch):
    _, wiki = isolated_home
    import deja.identity as user_mod
    import deja.config as config
    monkeypatch.setattr(user_mod, "WIKI_DIR", wiki)
    monkeypatch.setattr(config, "USER_SLUG", "custom-slug")

    _write_self_page(wiki, "custom-slug",
        "---\nemail: x@y.com\n---\n# Custom Person\n\nBio here.\n"
    )
    # Also drop a decoy page with self:true that should NOT be picked
    _write_self_page(wiki, "decoy",
        "---\nself: true\nemail: decoy@x.com\n---\n# Decoy\n\nNo.\n"
    )

    user = load_user()
    assert user.slug == "custom-slug"
    assert user.email == "x@y.com"


def test_load_user_first_name_from_full_name_when_no_preferred(isolated_home, monkeypatch):
    _, wiki = isolated_home
    import deja.identity as user_mod
    monkeypatch.setattr(user_mod, "WIKI_DIR", wiki)

    _write_self_page(wiki, "bob-smith",
        "---\nself: true\nemail: bob@x.com\n---\n# Bob Smith\n\nHi.\n"
    )
    user = load_user()
    assert user.first_name == "Bob"


# ---------------------------------------------------------------------------
# load_user — fallback and missing-field cases
# ---------------------------------------------------------------------------

def test_load_user_falls_back_to_generic_when_no_self_page(isolated_home, monkeypatch):
    _, wiki = isolated_home
    import deja.identity as user_mod
    import deja.config as config
    monkeypatch.setattr(user_mod, "WIKI_DIR", wiki)
    monkeypatch.setattr(config, "USER_SLUG", "")
    (wiki / "people").mkdir()  # empty dir, no pages

    user = load_user()
    assert user.is_generic
    assert user.name == "the user"
    assert user.first_name == "the user"
    assert user.email == ""
    assert user.phone == ""


def test_load_user_generic_when_people_dir_missing(isolated_home, monkeypatch):
    _, wiki = isolated_home
    import deja.identity as user_mod
    import deja.config as config
    monkeypatch.setattr(user_mod, "WIKI_DIR", wiki)
    monkeypatch.setattr(config, "USER_SLUG", "")
    # Don't create people/ at all

    user = load_user()
    assert user.is_generic


def test_load_user_missing_email_still_returns_profile(isolated_home, monkeypatch):
    """A self-page with no email should still produce a usable profile —
    the startup check catches the missing email separately."""
    _, wiki = isolated_home
    import deja.identity as user_mod
    monkeypatch.setattr(user_mod, "WIKI_DIR", wiki)

    _write_self_page(wiki, "no-email",
        "---\nself: true\n---\n# No Email\n\nJust a bio.\n"
    )
    user = load_user()
    assert user.slug == "no-email"
    assert user.name == "No Email"
    assert user.email == ""
    assert not user.is_generic


def test_as_prompt_fields_has_expected_keys(isolated_home, monkeypatch):
    _, wiki = isolated_home
    import deja.identity as user_mod
    monkeypatch.setattr(user_mod, "WIKI_DIR", wiki)
    _write_self_page(wiki, "test-user",
        "---\nself: true\nemail: t@u.com\npreferred_name: Tina\n---\n# Test User\n\nBio.\n"
    )
    fields = load_user().as_prompt_fields()
    assert set(fields.keys()) == {"user_name", "user_first_name", "user_email", "user_profile"}
    assert fields["user_name"] == "Test User"
    assert fields["user_first_name"] == "Tina"
    assert fields["user_email"] == "t@u.com"
    assert "Bio." in fields["user_profile"]


def test_as_prompt_fields_generic_has_placeholder_profile(isolated_home, monkeypatch):
    _, wiki = isolated_home
    import deja.identity as user_mod
    import deja.config as config
    monkeypatch.setattr(user_mod, "WIKI_DIR", wiki)
    monkeypatch.setattr(config, "USER_SLUG", "")

    fields = load_user().as_prompt_fields()
    assert fields["user_name"] == "the user"
    # Fallback profile should instruct the operator how to fix it
    assert "self-page" in fields["user_profile"].lower()
