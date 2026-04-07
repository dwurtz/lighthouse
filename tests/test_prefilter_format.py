"""llm.local: signal formatting and TRIAGE_SOURCES contract.

The triage_batch coroutine itself hits the network, so we only test the
pure-function building blocks.
"""

from __future__ import annotations

from deja.llm import prefilter as local_llm


def test_triage_sources_covers_messages():
    assert "imessage" in local_llm.TRIAGE_SOURCES
    assert "whatsapp" in local_llm.TRIAGE_SOURCES
    assert "email" in local_llm.TRIAGE_SOURCES
    # Sources that must pass through untouched
    assert "screenshot" not in local_llm.TRIAGE_SOURCES
    assert "calendar" not in local_llm.TRIAGE_SOURCES
    assert "microphone" not in local_llm.TRIAGE_SOURCES


def test_format_signals_block_numbers_entries():
    items = [
        {"source": "imessage", "sender": "Alice", "text": "hi there"},
        {"source": "whatsapp", "sender": "Bob", "text": "yo"},
    ]
    block = local_llm._format_signals_block(items)
    lines = block.split("\n")
    assert lines[0].startswith("1. [imessage]")
    assert "Alice" in lines[0]
    assert lines[1].startswith("2. [whatsapp]")
    assert "Bob" in lines[1]


def test_format_signals_block_truncates_long_text():
    items = [{"source": "imessage", "sender": "A", "text": "x" * 2000}]
    block = local_llm._format_signals_block(items)
    # Should be capped at 600 chars of text plus some prefix
    assert len(block) < 800


def test_format_signals_block_handles_newlines():
    items = [{"source": "imessage", "sender": "A\nB", "text": "line1\nline2"}]
    block = local_llm._format_signals_block(items)
    # Sender and text should be collapsed onto one line
    assert block.count("\n") == 0
    assert "A B" in block
    assert "line1 line2" in block


def test_format_signals_empty_list():
    assert local_llm._format_signals_block([]) == ""


def test_load_index_md_missing_returns_empty(isolated_home):
    # index.md doesn't exist in the isolated wiki
    assert local_llm._load_index_md() == ""


def test_load_index_md_reads_file(isolated_home):
    _, wiki = isolated_home
    (wiki / "index.md").write_text("# Index\n- [[jane-doe]]\n")
    assert "jane-doe" in local_llm._load_index_md()
