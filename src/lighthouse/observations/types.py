"""Observation type for all collectors.

An Observation is one atomic piece of context captured from the user's
digital environment: an iMessage, a browser visit, a clipboard copy, a
screenshot description, etc. The agent loop observes continuously,
integrates every few minutes, and reflects once a day — and observations
are the raw fuel at the bottom of that stack.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping


@dataclass
class Observation:
    """A single piece of context collected from the user's digital life."""

    source: str  # "imessage", "whatsapp", "chrome", "clipboard", "screenshot", "calendar", "active_app", "email", "drive", "tasks", "microphone", "conversation"
    sender: str  # person name, app name, or URL
    text: str  # content (truncated for storage)
    timestamp: datetime
    id_key: str  # dedup identifier


# Legacy alias — some internal code and old persisted data used `Signal`.
# Kept so imports from pre-rename pickles / call sites still resolve. New
# code should use `Observation` directly.
Signal = Observation


def is_outbound(obs: "Observation | Mapping") -> bool:
    """Return True if this observation is a message the user sent (not received).

    Outbound messages carry categorically higher intent than inbound:
    every one is deliberately composed, every one reveals what the user
    is committing to, deciding, or caring about — in their own voice.
    Downstream stages (prefilter, retrieval, integration, reflection)
    branch on this to give user outbound preferential treatment over
    the inbound firehose.

    Each collector marks outbound via a different string convention
    (see below); this helper is the one place those conventions are
    interpreted. Keep it in sync with collector code.

      • iMessage  — ``observations/imessage.py`` rewrites ``is_from_me=1``
        to ``sender = "You"``
      • WhatsApp  — same pattern, sender rewritten to "You"
      • Email     — ``observations/email.py`` sets
        ``sender = "<User Name> → …"`` and prefixes ``text`` with ``[SENT]``
      • Vision    — screen-description prompt prefixes ``[SENT]`` when the
        user is observed composing/sending in a messaging UI

    Accepts either an ``Observation`` dataclass or a plain dict (the
    integration loop reads structured observations from the log as dicts).
    """
    if isinstance(obs, Observation):
        source = obs.source
        sender = obs.sender or ""
        text = obs.text or ""
    else:
        source = obs.get("source", "") or ""
        sender = obs.get("sender", "") or ""
        text = obs.get("text", "") or ""

    if source in ("imessage", "whatsapp"):
        return sender == "You"
    if source == "email":
        # Any "<name> → recipient" pattern is outbound (avoids hardcoding
        # the user's name here — identity.load_user() is where names live).
        return "→" in sender or text.startswith("[SENT]")
    if source == "screenshot":
        return text.startswith("[SENT]")
    return False
