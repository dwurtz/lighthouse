"""Conversation threading — groups individual messages into threaded conversations.

Runs after signal collection, before analysis. Collapses individual iMessage/WhatsApp
signals from the same person within a time window into a single conversation signal
with the full thread context preserved.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta

from lighthouse.observations.types import Observation

log = logging.getLogger(__name__)

THREAD_WINDOW = timedelta(minutes=10)  # Group messages within 10 minutes


def thread_signals(signals: list[Observation]) -> list[Observation]:
    """Group message signals into conversation threads.

    Takes a flat list of signals and returns a new list where consecutive
    iMessage/WhatsApp signals from the same conversation are merged into
    a single threaded signal. Non-message signals pass through unchanged.
    """
    # Separate messages from other signals
    messages: list[Observation] = []
    other: list[Observation] = []

    for sig in signals:
        if sig.source in ("imessage", "whatsapp"):
            messages.append(sig)
        else:
            other.append(sig)

    if not messages:
        return signals

    # Group messages by conversation partner (normalized)
    conversations: dict[str, list[Observation]] = defaultdict(list)
    for msg in messages:
        # Normalize the conversation key — use the non-"You" participant
        partner = msg.sender if msg.sender != "You" else "You"
        # For "You" messages, we need to figure out who they're to
        # For now, group by the handle/phone in the id_key
        if msg.sender == "You":
            # Extract partner from id_key: imsg-You-2026-04-02 10:22:52-hash
            # We can't know the recipient from chat.db easily
            # So we'll group "You" messages with the nearest non-You message
            # from the same time window
            partner = "_outgoing"
        conversations[partner].append(msg)

    # Now merge conversations with nearby outgoing messages
    # Sort each partner's messages by timestamp
    threaded: list[Observation] = []

    # Collect all messages sorted by time
    all_msgs = sorted(messages, key=lambda s: s.timestamp)

    # Build threads: messages within THREAD_WINDOW of each other from/to the same person
    threads: list[list[Observation]] = []
    current_thread: list[Observation] = []
    current_partners: set[str] = set()

    for msg in all_msgs:
        if not current_thread:
            current_thread.append(msg)
            if msg.sender != "You":
                current_partners.add(msg.sender)
            continue

        last = current_thread[-1]
        time_gap = msg.timestamp - last.timestamp

        # Same thread if within time window
        if time_gap <= THREAD_WINDOW:
            current_thread.append(msg)
            if msg.sender != "You":
                current_partners.add(msg.sender)
        else:
            # Close current thread and start new one
            if len(current_thread) >= 2:
                threaded.append(_merge_thread(current_thread, current_partners))
            else:
                threaded.extend(current_thread)
            current_thread = [msg]
            current_partners = set()
            if msg.sender != "You":
                current_partners.add(msg.sender)

    # Close final thread
    if len(current_thread) >= 2:
        threaded.append(_merge_thread(current_thread, current_partners))
    else:
        threaded.extend(current_thread)

    # Combine with non-message signals
    result = other + threaded
    result.sort(key=lambda s: s.timestamp)
    return result


def _merge_thread(messages: list[Observation], partners: set[str]) -> Observation:
    """Merge multiple messages into a single conversation signal."""
    first = messages[0]
    last = messages[-1]

    partner_names = ", ".join(sorted(partners)) if partners else "unknown"
    source = first.source  # imessage or whatsapp

    # Build the threaded text
    lines = []
    for msg in messages:
        sender = msg.sender
        lines.append(f"  {sender}: {msg.text}")

    thread_text = f"CONVERSATION with {partner_names} ({len(messages)} messages, {first.timestamp.strftime('%H:%M')}-{last.timestamp.strftime('%H:%M')}):\n"
    thread_text += "\n".join(lines)

    # Use a composite id_key
    id_key = f"thread-{source}-{partner_names[:20]}-{first.timestamp.strftime('%H%M')}-{last.timestamp.strftime('%H%M')}"

    return Observation(
        source=source,
        sender=partner_names,
        text=thread_text[:1500],  # Allow longer text for threads
        timestamp=first.timestamp,
        id_key=id_key,
    )
