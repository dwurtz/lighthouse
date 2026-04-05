"""log.md formatting — the grep-stable bullet prefix matters."""

from __future__ import annotations

from lighthouse.activity_log import _ENTRY_PREFIX, append_log_entry, read_recent_log


def test_append_creates_file_with_preamble(isolated_home):
    _, wiki = isolated_home
    log_path = wiki / "log.md"
    assert not log_path.exists()
    append_log_entry("cycle", "first entry")
    assert log_path.exists()
    text = log_path.read_text()
    assert "# Log" in text
    assert _ENTRY_PREFIX in text
    assert "cycle — first entry" in text


def test_append_collapses_multiline(isolated_home):
    _, wiki = isolated_home
    append_log_entry("cycle", "line one\nline two\n\nline three")
    text = (wiki / "log.md").read_text()
    # Only one entry line should have been added
    entry_lines = [ln for ln in text.splitlines() if ln.startswith(_ENTRY_PREFIX)]
    assert len(entry_lines) == 1
    assert "line one line two line three" in entry_lines[0]


def test_append_is_append_only(isolated_home):
    _, wiki = isolated_home
    append_log_entry("cycle", "a")
    append_log_entry("startup", "b")
    append_log_entry("nightly", "c")
    text = (wiki / "log.md").read_text()
    entry_lines = [ln for ln in text.splitlines() if ln.startswith(_ENTRY_PREFIX)]
    assert len(entry_lines) == 3
    # Order preserved
    assert "cycle — a" in entry_lines[0]
    assert "startup — b" in entry_lines[1]
    assert "nightly — c" in entry_lines[2]


def test_read_recent_log_returns_last_n(isolated_home):
    for i in range(10):
        append_log_entry("cycle", f"entry {i}")
    recent = read_recent_log(max_entries=3)
    lines = recent.splitlines()
    assert len(lines) == 3
    assert "entry 9" in lines[-1]
    assert "entry 7" in lines[0]


def test_read_recent_log_empty_when_missing(isolated_home):
    assert read_recent_log() == ""


def test_append_swallows_exception_on_bad_path(isolated_home, monkeypatch):
    # Simulate a disk failure — append should never raise.
    import lighthouse.activity_log as wl
    monkeypatch.setattr(wl, "LOG_PATH", wl.LOG_PATH.parent / "ro" / "log.md")
    # Don't create the parent — write should fail silently
    append_log_entry("cycle", "test")  # must not raise
