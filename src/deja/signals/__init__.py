"""Signal tiering, triage, and formatting for the integrate cycle.

Three tiers, in priority order:

* **Tier 1 — Voice.** Anything the user authored themselves (outbound
  email / iMessage / WhatsApp, typed content, voice dictation, manual
  Obsidian edits) OR anything authored by a person flagged
  ``inner_circle: true`` on their ``people/<slug>.md`` frontmatter.
  These are the highest-signal observations in the system; missing one
  is categorically worse than keeping an irrelevant one.

* **Tier 2 — Attention.** Screenshots where the user deliberately
  engaged a single content view (an opened email, a specific doc, a
  specific message thread). The dwell-based collector already drops
  transient views, so any screenshot that arrives here is something the
  user paused on long enough to matter.

* **Tier 3 — Ambient.** Everything else — inbox lists, received bulk
  email, notifications, clipboard snapshots, browser navigation chatter.

The split exists so the integrate prompt can tell the LLM what the user
is actually *telling* us (Tier 1) apart from what merely crossed their
screen (Tier 3), without having to infer priority from source strings.
"""

from __future__ import annotations

from deja.signals.format import format_signals
from deja.signals.tiering import classify_tier, load_inner_circle_slugs
from deja.signals.triage import triage_signals

__all__ = [
    "classify_tier",
    "format_signals",
    "load_inner_circle_slugs",
    "triage_signals",
]
