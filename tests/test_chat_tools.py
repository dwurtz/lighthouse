"""Chat tool surface — validation, path safety, dispatch.

The tools are what the chat agent can actually DO to the wiki, so these
tests lock the guardrails: category/slug validation, path traversal
refusal, required reason strings, rename atomicity, legacy-arg handling.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def ct(isolated_home, monkeypatch):
    """Chat tools module with WIKI_DIR patched to the tmp wiki."""
    _, wiki = isolated_home
    import lighthouse.chat_tools as ct_mod
    import lighthouse.wiki as wiki_mod
    monkeypatch.setattr(ct_mod, "WIKI_DIR", wiki)
    monkeypatch.setattr(wiki_mod, "WIKI_DIR", wiki)
    # Neutralize git so writes don't try to commit
    monkeypatch.setattr("lighthouse.wiki_git.ensure_repo", lambda: None)
    monkeypatch.setattr("lighthouse.wiki_git.commit_changes", lambda msg: None)
    import lighthouse.wiki_catalog as wc
    monkeypatch.setattr(wc, "rebuild_index", lambda: 0)
    # wiki.apply_updates imports search.refresh_index lazily
    import lighthouse.llm.search as search
    monkeypatch.setattr(search, "refresh_index", lambda: None)
    (wiki / "people").mkdir()
    (wiki / "projects").mkdir()
    return ct_mod, wiki


def _seed(wiki, category, slug, body):
    (wiki / category / f"{slug}.md").write_text(body)


# ---------------------------------------------------------------------------
# list_pages
# ---------------------------------------------------------------------------

def test_list_pages_returns_all(ct):
    ct_mod, wiki = ct
    _seed(wiki, "people", "jane-doe", "# Jane Doe\n\nHi.\n")
    _seed(wiki, "projects", "acme", "# Acme\n\nProject.\n")

    r = ct_mod.list_pages()
    assert r.ok
    slugs = {p["slug"] for p in r.data["pages"]}
    assert slugs == {"jane-doe", "acme"}


def test_list_pages_filters_by_category(ct):
    ct_mod, wiki = ct
    _seed(wiki, "people", "jane-doe", "# Jane Doe\n")
    _seed(wiki, "projects", "acme", "# Acme\n")

    r = ct_mod.list_pages(category="people")
    assert r.ok
    assert len(r.data["pages"]) == 1
    assert r.data["pages"][0]["slug"] == "jane-doe"


def test_list_pages_extracts_h1_title(ct):
    ct_mod, wiki = ct
    _seed(wiki, "people", "jd", "---\nalias: foo\n---\n# Jane Roberta Doe\n\nHi.\n")
    r = ct_mod.list_pages()
    assert r.data["pages"][0]["title"] == "Jane Roberta Doe"


def test_list_pages_rejects_bad_category(ct):
    ct_mod, _ = ct
    r = ct_mod.list_pages(category="tasks")
    assert not r.ok
    assert "people" in r.message or "projects" in r.message


# ---------------------------------------------------------------------------
# read_page
# ---------------------------------------------------------------------------

def test_read_page_returns_content(ct):
    ct_mod, wiki = ct
    _seed(wiki, "people", "jane-doe", "# Jane\n\nBio.\n")
    r = ct_mod.read_page("people", "jane-doe")
    assert r.ok
    assert r.data["exists"] is True
    assert "Bio." in r.data["content"]


def test_read_page_missing_is_ok_with_exists_false(ct):
    """Reading a nonexistent page is not an error — it tells the model
    whether it needs to create vs update."""
    ct_mod, _ = ct
    r = ct_mod.read_page("people", "ghost")
    assert r.ok
    assert r.data["exists"] is False
    assert r.data["content"] == ""


def test_read_page_rejects_bad_slug(ct):
    ct_mod, _ = ct
    r = ct_mod.read_page("people", "Not Kebab Case")
    assert not r.ok


def test_read_page_rejects_path_traversal(ct):
    ct_mod, _ = ct
    # The slug validator blocks this before the path resolver sees it
    r = ct_mod.read_page("people", "../../etc/passwd")
    assert not r.ok


# ---------------------------------------------------------------------------
# write_page
# ---------------------------------------------------------------------------

def test_write_page_creates_new(ct):
    ct_mod, wiki = ct
    r = ct_mod.write_page(
        "people", "jane-doe",
        "# Jane Doe\n\nA new person.\n",
        reason="user asked me to add Jane",
    )
    assert r.ok
    assert r.data["was_new"] is True
    assert (wiki / "people" / "jane-doe.md").exists()


def test_write_page_overwrites_existing(ct):
    ct_mod, wiki = ct
    _seed(wiki, "people", "jane-doe", "# Old\n")
    r = ct_mod.write_page(
        "people", "jane-doe",
        "# Jane Doe\n\nRewritten.\n",
        reason="correcting outdated bio",
    )
    assert r.ok
    assert r.data["was_new"] is False
    assert "Rewritten" in (wiki / "people" / "jane-doe.md").read_text()


def test_write_page_requires_reason(ct):
    ct_mod, _ = ct
    r = ct_mod.write_page("people", "jane-doe", "# Jane\n", reason="")
    assert not r.ok
    assert "reason" in r.message.lower()


def test_write_page_requires_content(ct):
    ct_mod, _ = ct
    r = ct_mod.write_page("people", "jane-doe", "", reason="test")
    assert not r.ok
    assert "content" in r.message.lower()


def test_write_page_rejects_bad_category(ct):
    ct_mod, _ = ct
    r = ct_mod.write_page("tasks", "foo", "# X\n", reason="test")
    assert not r.ok


# ---------------------------------------------------------------------------
# delete_page
# ---------------------------------------------------------------------------

def test_delete_page_removes_existing(ct):
    ct_mod, wiki = ct
    _seed(wiki, "people", "old", "# Old\n\nGone.\n")
    r = ct_mod.delete_page("people", "old", reason="user said this isn't real")
    assert r.ok
    assert not (wiki / "people" / "old.md").exists()


def test_delete_page_missing_is_noop(ct):
    ct_mod, _ = ct
    r = ct_mod.delete_page("people", "ghost", reason="cleanup")
    assert r.ok  # no-op is success
    assert "did not exist" in r.message or "no-op" in r.message


def test_delete_page_requires_reason(ct):
    ct_mod, wiki = ct
    _seed(wiki, "people", "jane", "# Jane\n")
    r = ct_mod.delete_page("people", "jane", reason="")
    assert not r.ok
    # File still on disk
    assert (wiki / "people" / "jane.md").exists()


# ---------------------------------------------------------------------------
# rename_page
# ---------------------------------------------------------------------------

def test_rename_page_moves_content(ct):
    ct_mod, wiki = ct
    _seed(wiki, "people", "old-slug", "# Old Slug\n\nBody here.\n")
    r = ct_mod.rename_page(
        "people", "old-slug", "new-slug",
        reason="cleaner name",
    )
    assert r.ok
    assert not (wiki / "people" / "old-slug.md").exists()
    assert (wiki / "people" / "new-slug.md").exists()
    assert "Body here." in (wiki / "people" / "new-slug.md").read_text()


def test_rename_page_refuses_if_target_exists(ct):
    """Merges require write + delete, not rename."""
    ct_mod, wiki = ct
    _seed(wiki, "people", "a", "# A\n")
    _seed(wiki, "people", "b", "# B\n")
    r = ct_mod.rename_page("people", "a", "b", reason="merge a into b")
    assert not r.ok
    assert "already exists" in r.message


def test_rename_page_refuses_if_source_missing(ct):
    ct_mod, _ = ct
    r = ct_mod.rename_page("people", "ghost", "new-name", reason="test")
    assert not r.ok
    assert "does not exist" in r.message


def test_rename_page_refuses_self_rename(ct):
    ct_mod, wiki = ct
    _seed(wiki, "people", "jane", "# Jane\n")
    r = ct_mod.rename_page("people", "jane", "jane", reason="no-op")
    assert not r.ok


# ---------------------------------------------------------------------------
# execute_tool_call — dispatch layer
# ---------------------------------------------------------------------------

def test_execute_tool_call_routes_to_list_pages(ct):
    ct_mod, _ = ct
    r = ct_mod.execute_tool_call("list_pages", {})
    assert r.ok


def test_execute_tool_call_unknown_tool(ct):
    ct_mod, _ = ct
    r = ct_mod.execute_tool_call("format_hard_drive", {})
    assert not r.ok
    assert "unknown" in r.message.lower()


def test_execute_tool_call_bad_args(ct):
    ct_mod, _ = ct
    r = ct_mod.execute_tool_call("write_page", {"category": "people"})  # missing slug/content/reason
    assert not r.ok


def test_execute_tool_call_never_raises(ct):
    """Even an exception inside a tool becomes a ToolResult, so Pro can recover."""
    ct_mod, _ = ct
    # Induce a crash by passing a non-string slug
    r = ct_mod.execute_tool_call("read_page", {"category": "people", "slug": 12345})
    assert isinstance(r, ct_mod.ToolResult)
    assert not r.ok


# ---------------------------------------------------------------------------
# build_tool_declarations — SDK binding
# ---------------------------------------------------------------------------

def test_build_tool_declarations_covers_all_tools(ct):
    ct_mod, _ = ct
    tools = ct_mod.build_tool_declarations()
    assert len(tools) == 1
    names = {fd.name for fd in tools[0].function_declarations}
    assert names == {"list_pages", "read_page", "write_page", "delete_page", "rename_page"}
