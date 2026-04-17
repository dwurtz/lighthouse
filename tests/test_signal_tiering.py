"""Tier classification tests for the per-turn messaging contract.

These exercise the post-migration behaviour: tiering now keys off the
per-turn ``speaker`` field (the single participant who authored this
specific turn) rather than ``sender`` (which for group chats is a
joined chat label). A group chat with an inner-circle member gets Tier
1 only on that member's own turns — other participants' turns are not
upgraded just because they share a room.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from deja.signals import tiering


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def inner_circle_wiki(isolated_home, monkeypatch):
    """Populate ``people/`` with one inner-circle entry (Dominique) and
    reset tiering caches so the test picks up the fresh frontmatter."""
    _, wiki = isolated_home
    # tiering.py captured WIKI_DIR at import time — patch its local
    # binding too so the test's isolated wiki actually gets scanned.
    monkeypatch.setattr(tiering, "WIKI_DIR", wiki)
    people = wiki / "people"
    people.mkdir()
    (people / "dominique.md").write_text(
        "---\n"
        "name: Dominique\n"
        "inner_circle: true\n"
        "phones: ['+15551112222']\n"
        "emails: ['dominique@example.com']\n"
        "---\n"
        "# Dominique\n"
    )
    (people / "laura.md").write_text(
        "---\n"
        "name: Laura\n"
        "inner_circle: false\n"
        "phones: ['+15553334444']\n"
        "---\n"
        "# Laura\n"
    )
    tiering.reset_caches()
    yield wiki
    tiering.reset_caches()


# ---------------------------------------------------------------------------
# Group-chat per-turn classification
# ---------------------------------------------------------------------------


def test_group_turn_from_inner_circle_is_tier1(inner_circle_wiki):
    """Dominique's own turn in a mixed group chat → Tier 1."""
    obs = {
        "source": "imessage",
        "sender": "Nie",
        "chat_id": "imsg-chat-42",
        "chat_label": "Nie",
        "speaker": "Dominique (+15551112222)",
        "text": "thanks for sending along Sansita's address",
    }
    assert tiering.classify_tier(obs) == 1


def test_group_turn_from_non_inner_circle_is_tier3(inner_circle_wiki):
    """Laura's turn in the SAME group chat → Tier 3 (despite Dominique
    being a member, the turn's speaker is what matters)."""
    obs = {
        "source": "imessage",
        "sender": "Nie",
        "chat_id": "imsg-chat-42",
        "chat_label": "Nie",
        "speaker": "Laura (+15553334444)",
        "text": "here's Sansita's address: 123 Main St",
    }
    # Laura's phone isn't in an inner-circle page, so this is Tier 3.
    assert tiering.classify_tier(obs) == 3


def test_group_outbound_is_tier1(inner_circle_wiki):
    """The user's own turn in a group → Tier 1 (speaker == 'You')."""
    obs = {
        "source": "whatsapp",
        "sender": "Family",
        "chat_id": "wa-chat-7",
        "chat_label": "Family",
        "speaker": "You",
        "text": "on my way",
    }
    assert tiering.classify_tier(obs) == 1


def test_imessage_outbound_uses_speaker_not_sender(inner_circle_wiki):
    """Sender is the chat label — must not be used for the outbound test.
    Only ``speaker == 'You'`` should trigger Tier 1."""
    obs = {
        "source": "imessage",
        "sender": "Nie",          # chat label, not "You"
        "chat_id": "imsg-chat-42",
        "chat_label": "Nie",
        "speaker": "You",
        "text": "ok",
    }
    assert tiering.classify_tier(obs) == 1


# ---------------------------------------------------------------------------
# 1:1 back-compat
# ---------------------------------------------------------------------------


def test_one_to_one_inner_circle_inbound_still_tier1(inner_circle_wiki):
    """A direct 1:1 inbound turn from the inner-circle speaker is Tier 1
    (same behaviour as before the migration)."""
    obs = {
        "source": "imessage",
        "sender": "Dominique (+15551112222)",
        "chat_id": "imsg-chat-9",
        "chat_label": "Dominique (+15551112222)",
        "speaker": "Dominique (+15551112222)",
        "text": "hey",
    }
    assert tiering.classify_tier(obs) == 1


def test_legacy_observation_without_speaker_falls_back_to_sender(inner_circle_wiki):
    """Pre-migration rows in observations.jsonl have no ``speaker``. The
    classifier must still tier them using ``sender`` so existing history
    renders with the correct priority (back-compat)."""
    legacy = {
        "source": "imessage",
        "sender": "You",
        "text": "hello from the past",
    }
    assert tiering.classify_tier(legacy) == 1

    legacy_inner = {
        "source": "imessage",
        "sender": "Dominique (+15551112222)",
        "text": "old message",
    }
    assert tiering.classify_tier(legacy_inner) == 1

    legacy_outsider = {
        "source": "imessage",
        "sender": "Random (+15559999999)",
        "text": "cold outreach",
    }
    assert tiering.classify_tier(legacy_outsider) == 3


# ---------------------------------------------------------------------------
# Email (speaker field doesn't apply — unchanged)
# ---------------------------------------------------------------------------


def test_email_sent_is_tier1_regardless_of_speaker(inner_circle_wiki):
    obs = {
        "source": "email",
        "sender": "you@me.com → bob@example.com",
        "text": "[SENT] see attached",
    }
    assert tiering.classify_tier(obs) == 1


def test_email_engaged_is_tier1(inner_circle_wiki):
    obs = {
        "source": "email",
        "sender": "stranger@example.com → you@me.com",
        "text": "[ENGAGED] here's the spec",
    }
    assert tiering.classify_tier(obs) == 1


def test_email_inner_circle_inbound_is_tier1(inner_circle_wiki):
    obs = {
        "source": "email",
        "sender": "Dominique <dominique@example.com>",
        "text": "hey, here's the doc",
    }
    assert tiering.classify_tier(obs) == 1
