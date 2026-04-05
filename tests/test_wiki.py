"""wiki.apply_updates routing and write_page invariants."""

from __future__ import annotations

from lighthouse import wiki as wiki_mod


def test_slugify_normalizes():
    assert wiki_mod.slugify("Hello World!") == "hello-world"
    assert wiki_mod.slugify("  Foo/Bar  ") == "foo-bar"
    assert wiki_mod.slugify("") == "unnamed"


def test_write_page_and_read_all(isolated_home, monkeypatch):
    # Neutralize git / index / QMD side effects — we're testing routing only.
    monkeypatch.setattr("lighthouse.wiki_git.ensure_repo", lambda: None)
    monkeypatch.setattr("lighthouse.wiki_git.commit_changes", lambda msg: None)

    wiki_mod.write_page("people", "Jane Doe", "# Jane\n\nNotes.")
    pages = wiki_mod.read_all_pages()
    assert len(pages) == 1
    assert pages[0]["category"] == "people"
    assert pages[0]["slug"] == "jane-doe"
    assert pages[0]["title"] == "Jane"


def test_write_page_rejects_bad_category(isolated_home):
    import pytest
    with pytest.raises(ValueError):
        wiki_mod.write_page("tasks", "x", "content")


def test_apply_updates_skips_invalid(isolated_home, monkeypatch):
    monkeypatch.setattr("lighthouse.wiki_git.ensure_repo", lambda: None)
    monkeypatch.setattr("lighthouse.wiki_git.commit_changes", lambda msg: None)
    monkeypatch.setattr("lighthouse.wiki_catalog.rebuild_index", lambda: None)
    # wiki.apply_updates imports refresh_index lazily — stub at the source.
    import lighthouse.llm.search as search
    monkeypatch.setattr(search, "refresh_index", lambda: None)

    updates = [
        {"category": "people", "slug": "ok", "content": "# OK\n", "action": "create", "reason": "r"},
        {"category": "nonsense", "slug": "bad", "content": "x", "action": "create", "reason": "r"},
        {"category": "projects", "slug": "", "content": "x", "action": "create", "reason": "r"},
        {"category": "projects", "slug": "no-content", "content": "", "action": "create", "reason": "r"},
    ]
    applied = wiki_mod.apply_updates(updates)
    assert applied == 1
    pages = wiki_mod.read_all_pages()
    assert len(pages) == 1
    assert pages[0]["slug"] == "ok"


def test_apply_updates_empty_list_noop(isolated_home):
    assert wiki_mod.apply_updates([]) == 0


def _stub_side_effects(monkeypatch):
    monkeypatch.setattr("lighthouse.wiki_git.ensure_repo", lambda: None)
    monkeypatch.setattr("lighthouse.wiki_git.commit_changes", lambda msg: None)
    monkeypatch.setattr("lighthouse.wiki_catalog.rebuild_index", lambda: None)
    import lighthouse.llm.search as search
    monkeypatch.setattr(search, "refresh_index", lambda: None)


def test_delete_page_removes_file_and_returns_true(isolated_home, monkeypatch):
    _stub_side_effects(monkeypatch)
    wiki_mod.write_page("projects", "terafab", "# Terafab\n\nA fleeting interest.")
    assert (wiki_mod.WIKI_DIR / "projects" / "terafab.md").exists()
    assert wiki_mod.delete_page("projects", "terafab") is True
    assert not (wiki_mod.WIKI_DIR / "projects" / "terafab.md").exists()


def test_delete_page_missing_is_noop(isolated_home, monkeypatch):
    _stub_side_effects(monkeypatch)
    assert wiki_mod.delete_page("projects", "never-existed") is False


def test_delete_page_bad_category_raises(isolated_home):
    import pytest
    with pytest.raises(ValueError):
        wiki_mod.delete_page("nonsense", "x")


def test_apply_updates_delete_action_routes(isolated_home, monkeypatch):
    _stub_side_effects(monkeypatch)
    # Seed a page first.
    wiki_mod.write_page("projects", "terafab", "# Terafab\n\nContent.")
    assert (wiki_mod.WIKI_DIR / "projects" / "terafab.md").exists()

    logged: list[tuple[str, str]] = []
    import lighthouse.activity_log as activity_log
    monkeypatch.setattr(
        activity_log,
        "append_log_entry",
        lambda kind, msg: logged.append((kind, msg)),
    )

    applied = wiki_mod.apply_updates([
        {
            "category": "projects",
            "slug": "terafab",
            "action": "delete",
            "reason": "user said 'terafab was a fleeting interest, delete that page'",
        }
    ])
    assert applied == 1
    assert not (wiki_mod.WIKI_DIR / "projects" / "terafab.md").exists()
    # Loud activity log entry recorded under integrate.
    assert logged and logged[0][0] == "integrate"
    assert "terafab" in logged[0][1] and "fleeting" in logged[0][1]


def test_apply_updates_delete_nonexistent_is_noop(isolated_home, monkeypatch):
    _stub_side_effects(monkeypatch)
    applied = wiki_mod.apply_updates([
        {
            "category": "projects",
            "slug": "ghost-page",
            "action": "delete",
            "reason": "user asked",
        }
    ])
    # Nothing existed to delete → no applied writes.
    assert applied == 0


def test_apply_updates_delete_without_slug_skipped(isolated_home, monkeypatch):
    _stub_side_effects(monkeypatch)
    applied = wiki_mod.apply_updates([
        {"category": "projects", "slug": "", "action": "delete", "reason": "x"},
        {"category": "bogus", "slug": "foo", "action": "delete", "reason": "x"},
    ])
    assert applied == 0
