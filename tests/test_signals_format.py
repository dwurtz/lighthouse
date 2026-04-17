"""Thread reconstruction and format tests after the per-turn migration.

Covers:
  * ``_thread_key`` — keyed on chat_id for new observations, falls back
    to sender for legacy rows.
  * Thread-context injection rebuilds a group conversation from per-turn
    observations with clear per-speaker attribution.
  * Legacy CONVERSATION-digest observations still parse into messages
    via the shim so ~20MB of historical observations.jsonl renders.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from deja.signals import format as fmt


# ---------------------------------------------------------------------------
# _is_threaded_source + _thread_identifiers
# ---------------------------------------------------------------------------


def test_threaded_source_identifies_messaging():
    assert fmt._is_threaded_source({"source": "imessage"})
    assert fmt._is_threaded_source({"source": "whatsapp"})
    assert not fmt._is_threaded_source({"source": "email"})
    assert not fmt._is_threaded_source({"source": "screenshot"})


def test_thread_identifiers_includes_chat_id_when_present():
    obs = {
        "source": "imessage",
        "sender": "Nie",
        "chat_id": "imsg-chat-42",
        "chat_label": "Nie",
        "speaker": "Laura (+15553334444)",
        "text": "hi",
    }
    idents = fmt._thread_identifiers(obs)
    assert "imsg-chat-42" in idents
    # sender + chat_label included too so legacy rows match cross-shape.
    assert "Nie" in idents


def test_thread_identifiers_stable_on_chat_id_across_label_change():
    """Two observations in the same chat with different chat_labels must
    still share at least one identifier (the chat_id)."""
    obs_a = {"source": "imessage", "sender": "Nie", "chat_id": "imsg-chat-42"}
    obs_b = {"source": "imessage", "sender": "Nie + 1", "chat_id": "imsg-chat-42"}
    assert fmt._thread_identifiers(obs_a) & fmt._thread_identifiers(obs_b)


def test_thread_identifiers_falls_back_to_sender_for_legacy():
    """Legacy row without chat_id → sender alone is the identifier."""
    obs = {"source": "imessage", "sender": "Alice"}
    assert fmt._thread_identifiers(obs) == {"Alice"}


def test_thread_identifiers_empty_when_no_fields():
    assert fmt._thread_identifiers({"source": "imessage"}) == set()


# ---------------------------------------------------------------------------
# _extract_messages
# ---------------------------------------------------------------------------


def test_extract_messages_per_turn_shape():
    obs = {
        "source": "imessage",
        "sender": "Nie",
        "chat_id": "imsg-chat-42",
        "speaker": "Dominique (+15551112222)",
        "text": "thanks",
    }
    pairs = fmt._extract_messages(obs)
    assert pairs == [("Dominique (+15551112222)", "thanks")]


def test_extract_messages_legacy_conversation_digest():
    """Legacy shim: CONVERSATION header gets peeled into per-message pairs."""
    obs = {
        "source": "imessage",
        "sender": "Alice",
        "text": (
            "CONVERSATION with Alice, Bob (3 messages, 10:00-10:05):\n"
            "  You: hey\n"
            "  Alice: hi\n"
            "  Bob: yo"
        ),
    }
    pairs = fmt._extract_messages(obs)
    assert ("You", "hey") in pairs
    assert ("Alice", "hi") in pairs
    assert ("Bob", "yo") in pairs


def test_extract_messages_single_legacy_row():
    """Legacy single-row: no speaker, no digest header → empty-speaker pair."""
    obs = {"source": "imessage", "sender": "Alice", "text": "hi there"}
    pairs = fmt._extract_messages(obs)
    assert pairs == [("", "hi there")]


# ---------------------------------------------------------------------------
# Thread context injection (end-to-end with observations.jsonl)
# ---------------------------------------------------------------------------


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def test_inject_thread_context_rebuilds_group_conversation(isolated_home, monkeypatch):
    """Three per-turn rows in one chat → Context block shows all prior
    turns with per-speaker attribution before the new turn renders."""
    home, _ = isolated_home
    jsonl = home / "observations.jsonl"

    now = datetime(2026, 4, 16, 10, 0, 0)

    def iso(offset_sec: int) -> str:
        return (now + timedelta(seconds=offset_sec)).isoformat()

    rows = [
        {
            "source": "imessage",
            "sender": "Nie",
            "chat_id": "imsg-chat-42",
            "chat_label": "Nie",
            "speaker": "Laura (+15553334444)",
            "text": "here's Sansita's address: 123 Main St",
            "timestamp": iso(0),
            "id_key": "imsg-chat-42-laura-0",
        },
        {
            "source": "imessage",
            "sender": "Nie",
            "chat_id": "imsg-chat-42",
            "chat_label": "Nie",
            "speaker": "You",
            "text": "thx",
            "timestamp": iso(30),
            "id_key": "imsg-chat-42-you-1",
        },
    ]
    _write_jsonl(jsonl, rows)

    # Patch the format module's OBSERVATIONS_LOG binding (it captured
    # the config value at import time).
    monkeypatch.setattr(fmt, "OBSERVATIONS_LOG", jsonl)

    current = {
        "source": "imessage",
        "sender": "Nie",
        "chat_id": "imsg-chat-42",
        "chat_label": "Nie",
        "speaker": "Dominique (+15551112222)",
        "text": "thanks for sending along Sansita's address",
        "timestamp": iso(60),
        "id_key": "imsg-chat-42-dominique-2",
    }

    augmented = fmt._inject_thread_context(current)
    out = augmented["text"]

    # Context block rendered with per-speaker labels.
    assert "## Context" in out
    assert "Laura (+15553334444):" in out
    assert "here's Sansita's address" in out
    assert "You: thx" in out
    # New section still carries the current turn.
    assert "## New this cycle" in out
    assert "thanks for sending along Sansita's address" in out


def test_inject_thread_context_honors_chat_id_not_sender(isolated_home, monkeypatch):
    """Prior observations with the SAME chat_id but a different sender
    string still land in the context (this is the bug the migration
    fixes)."""
    home, _ = isolated_home
    jsonl = home / "observations.jsonl"
    now = datetime(2026, 4, 16, 10, 0, 0).isoformat()

    # Prior row used an outdated sender (e.g. group renamed since) but
    # the chat_id is the stable session id.
    rows = [
        {
            "source": "imessage",
            "sender": "Old Group Name",
            "chat_id": "imsg-chat-42",
            "chat_label": "Old Group Name",
            "speaker": "Laura (+15553334444)",
            "text": "earlier message",
            "timestamp": now,
            "id_key": "prior-1",
        },
    ]
    _write_jsonl(jsonl, rows)
    monkeypatch.setattr(fmt, "OBSERVATIONS_LOG", jsonl)

    current = {
        "source": "imessage",
        "sender": "Nie (New Name)",
        "chat_id": "imsg-chat-42",
        "chat_label": "Nie (New Name)",
        "speaker": "Dominique (+15551112222)",
        "text": "current message",
        "timestamp": now,
        "id_key": "current-1",
    }
    augmented = fmt._inject_thread_context(current)
    assert "earlier message" in augmented["text"]


def test_legacy_conversation_digest_still_renders(isolated_home, monkeypatch):
    """A legacy CONVERSATION-digest row in observations.jsonl, combined
    with a new per-turn current observation, still threads together via
    the (source, sender) fallback — existing history must keep working."""
    home, _ = isolated_home
    jsonl = home / "observations.jsonl"
    now = datetime(2026, 4, 16, 9, 0, 0).isoformat()

    legacy_row = {
        "source": "imessage",
        "sender": "Alice",
        "text": (
            "CONVERSATION with Alice (2 messages, 09:00-09:01):\n"
            "  Alice: heya\n"
            "  You: morning"
        ),
        "timestamp": now,
        "id_key": "legacy-digest-1",
    }
    _write_jsonl(jsonl, [legacy_row])
    monkeypatch.setattr(fmt, "OBSERVATIONS_LOG", jsonl)

    # Current observation is also legacy (no chat_id) so it falls back
    # to sender-based threading — simulates the live app running the
    # OLD Swift with the NEW Python side.
    current = {
        "source": "imessage",
        "sender": "Alice",
        "text": "still there?",
        "timestamp": now,
        "id_key": "current-legacy",
    }
    augmented = fmt._inject_thread_context(current)
    # The digest should have been peeled into its constituent messages.
    assert "Alice: heya" in augmented["text"]
    assert "You: morning" in augmented["text"]


# ---------------------------------------------------------------------------
# format_signals end-to-end
# ---------------------------------------------------------------------------


def test_format_signals_renders_per_turn_with_chat_label(isolated_home, monkeypatch):
    """Group chat, 3 per-turn observations → rendered lines include the
    chat label AND the speaker, not just one joined string."""
    home, _ = isolated_home
    jsonl = home / "observations.jsonl"
    jsonl.write_text("")
    monkeypatch.setattr(fmt, "OBSERVATIONS_LOG", jsonl)

    sigs = [
        {
            "source": "imessage",
            "sender": "Nie",
            "chat_id": "imsg-chat-42",
            "chat_label": "Nie",
            "speaker": "Laura (+15553334444)",
            "text": "address is 123 Main St",
            "timestamp": "2026-04-16T10:00:00",
            "id_key": "a",
        },
        {
            "source": "imessage",
            "sender": "Nie",
            "chat_id": "imsg-chat-42",
            "chat_label": "Nie",
            "speaker": "Dominique (+15551112222)",
            "text": "thanks",
            "timestamp": "2026-04-16T10:01:00",
            "id_key": "b",
        },
    ]
    rendered = fmt.format_signals(sigs)
    # Each line carries the speaker, not just the chat label.
    assert "Nie / Laura (+15553334444):" in rendered
    assert "Nie / Dominique (+15551112222):" in rendered
