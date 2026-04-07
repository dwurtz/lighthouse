"""One-time onboarding jobs that bootstrap the wiki for a new user.

Onboarding runs a sequence of steps (sent email, iMessage, WhatsApp,
future additions) on first monitor start — in the background so the
notch and analysis loop come up immediately — to seed the wiki with
pages built from historical context rather than waiting days for the
steady-state loop to discover everything from scratch.

Progress is tracked in ``~/.deja/onboarding.json`` so each step
runs exactly once, and users can resume incomplete sequences (e.g.
after granting Full Disk Access for iMessage) without re-running
earlier steps.
"""

from deja.onboarding.backfill import (
    ALL_STEPS,
    backfill_calendar,
    backfill_imessage,
    backfill_meet_transcripts,
    backfill_sent_email,
    backfill_whatsapp,
    is_step_done,
    load_marker,
    mark_step_done,
    run_all_pending_steps,
)

__all__ = [
    "ALL_STEPS",
    "backfill_calendar",
    "backfill_imessage",
    "backfill_meet_transcripts",
    "backfill_sent_email",
    "backfill_whatsapp",
    "is_step_done",
    "load_marker",
    "mark_step_done",
    "run_all_pending_steps",
]
