"""Onboarding steps — one-time wiki bootstrap jobs.

Onboarding consists of a sequence of independent "steps," each of which
ingests one source of historical context and merges it into the wiki:

  * ``sent_email_backfill`` — last N days of sent Gmail threads (full
    thread fetched per hit so both sides of every conversation land in
    the wiki).
  * ``imessage_backfill`` — last N days of iMessage chats the user was
    active in, formatted as per-contact conversation digests.
  * ``whatsapp_backfill`` — same, for WhatsApp.

Progress is tracked in ``~/.deja/onboarding.json`` so steps only
run once unless ``--force`` is passed, and new users get all pending
steps run in sequence on first monitor startup.

Each step is a thin wrapper around ``runner.run_step``; the runner owns
all the shared mechanics (batching, locking, LLM calls, logging).
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

from deja.config import DEJA_HOME

log = logging.getLogger(__name__)


MARKER_PATH = DEJA_HOME / "onboarding.json"

# Ordered list of all onboarding steps. The startup hook and the CLI
# "run everything pending" path walk this list in order. Each entry is
# (step_name, human_description).
ALL_STEPS: list[tuple[str, str]] = [
    ("sent_email_backfill", "30 days of sent Gmail threads"),
    ("imessage_backfill", "30 days of iMessage conversations"),
    ("whatsapp_backfill", "30 days of WhatsApp conversations"),
    ("calendar_backfill", "30 days of calendar meetings + Granola notes"),
    ("meet_transcript_backfill", "30 days of Google Meet transcripts"),
]


# ---------------------------------------------------------------------------
# Marker file
# ---------------------------------------------------------------------------


def load_marker() -> dict[str, Any]:
    """Return the onboarding marker dict, or ``{}`` if no marker exists."""
    if not MARKER_PATH.exists():
        return {}
    try:
        return json.loads(MARKER_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        log.warning("onboarding marker at %s is corrupt, ignoring", MARKER_PATH)
        return {}


def is_step_done(step: str) -> bool:
    """True if ``step`` has been recorded as completed in the marker file."""
    marker = load_marker()
    return step in (marker.get("steps_done") or [])


def mark_step_done(step: str, details: dict[str, Any] | None = None) -> None:
    """Record that an onboarding step has finished. Idempotent."""
    marker = load_marker()
    steps = set(marker.get("steps_done") or [])
    steps.add(step)
    marker["steps_done"] = sorted(steps)
    marker.setdefault("history", []).append({
        "step": step,
        "at": datetime.now(timezone.utc).isoformat(),
        **(details or {}),
    })
    MARKER_PATH.parent.mkdir(parents=True, exist_ok=True)
    MARKER_PATH.write_text(json.dumps(marker, indent=2))


# ---------------------------------------------------------------------------
# Step: sent-email backfill
# ---------------------------------------------------------------------------


async def backfill_sent_email(
    *,
    days: int = 30,
    wiki_lock: asyncio.Lock | None = None,
    gemini: Any = None,
    force: bool = False,
    on_progress: Any = None,
) -> dict[str, Any]:
    """Ingest sent-mail threads from the last ``days`` days into the wiki."""
    from deja.observations.email import fetch_sent_threads_backfill
    from deja.onboarding.runner import run_step

    return await run_step(
        name="sent_email_backfill",
        fetch_fn=lambda: fetch_sent_threads_backfill(days=days),
        wiki_lock=wiki_lock,
        gemini=gemini,
        force=force,
        on_progress=on_progress,
    )


# ---------------------------------------------------------------------------
# Step: iMessage backfill
# ---------------------------------------------------------------------------


def _imessage_pre_check() -> dict[str, Any] | None:
    """Verify chat.db is readable. Returns a skip dict if not."""
    import sqlite3
    from deja.config import IMESSAGE_DB
    if not IMESSAGE_DB.exists():
        return {"skipped": "imessage_db_missing", "detail": str(IMESSAGE_DB)}
    try:
        conn = sqlite3.connect(f"file:{IMESSAGE_DB}?mode=ro", uri=True)
        conn.execute("SELECT 1").fetchone()
        conn.close()
    except sqlite3.Error as e:
        return {
            "skipped": "imessage_no_access",
            "detail": str(e),
            "fix": (
                "Grant Full Disk Access to the Déjà Python binary "
                "in System Settings → Privacy & Security → Full Disk Access, "
                "then re-run `deja onboard`."
            ),
        }
    return None


async def backfill_imessage(
    *,
    days: int = 30,
    messages_per_contact: int = 100,
    wiki_lock: asyncio.Lock | None = None,
    gemini: Any = None,
    force: bool = False,
    on_progress: Any = None,
) -> dict[str, Any]:
    """Ingest per-contact iMessage digests from the last ``days`` days."""
    from deja.observations.imessage import fetch_imessage_contacts_backfill
    from deja.onboarding.runner import run_step

    return await run_step(
        name="imessage_backfill",
        fetch_fn=lambda: fetch_imessage_contacts_backfill(
            days=days,
            messages_per_contact=messages_per_contact,
        ),
        wiki_lock=wiki_lock,
        gemini=gemini,
        force=force,
        on_progress=on_progress,
        pre_check=_imessage_pre_check,
    )


# ---------------------------------------------------------------------------
# Step: WhatsApp backfill
# ---------------------------------------------------------------------------


def _whatsapp_pre_check() -> dict[str, Any] | None:
    """Verify ChatStorage.sqlite is readable. Returns a skip dict if not."""
    import sqlite3
    from deja.config import WHATSAPP_DB
    if not WHATSAPP_DB.exists():
        return {"skipped": "whatsapp_db_missing", "detail": str(WHATSAPP_DB)}
    try:
        conn = sqlite3.connect(f"file:{WHATSAPP_DB}?mode=ro", uri=True)
        conn.execute("SELECT 1").fetchone()
        conn.close()
    except sqlite3.Error as e:
        return {
            "skipped": "whatsapp_no_access",
            "detail": str(e),
            "fix": (
                "Grant Full Disk Access to the Déjà Python binary "
                "in System Settings → Privacy & Security → Full Disk Access, "
                "then re-run `deja onboard`."
            ),
        }
    return None


async def backfill_whatsapp(
    *,
    days: int = 30,
    messages_per_contact: int = 100,
    wiki_lock: asyncio.Lock | None = None,
    gemini: Any = None,
    force: bool = False,
    on_progress: Any = None,
) -> dict[str, Any]:
    """Ingest per-contact WhatsApp digests from the last ``days`` days."""
    from deja.observations.whatsapp import fetch_whatsapp_contacts_backfill
    from deja.onboarding.runner import run_step

    return await run_step(
        name="whatsapp_backfill",
        fetch_fn=lambda: fetch_whatsapp_contacts_backfill(
            days=days,
            messages_per_contact=messages_per_contact,
        ),
        wiki_lock=wiki_lock,
        gemini=gemini,
        force=force,
        on_progress=on_progress,
        pre_check=_whatsapp_pre_check,
    )


# ---------------------------------------------------------------------------
# Run all pending steps (CLI default + startup hook)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Step: Calendar backfill (with Granola enrichment)
# ---------------------------------------------------------------------------


async def backfill_calendar(
    *,
    days: int = 30,
    wiki_lock: asyncio.Lock | None = None,
    gemini: Any = None,
    force: bool = False,
    on_progress: Any = None,
) -> dict[str, Any]:
    """Ingest past calendar meetings, enriched with Granola notes.

    Google Calendar is the source of truth for "what meetings happened."
    Granola is the enrichment layer that attaches notes/transcripts to
    matching events. The join happens inside ``enrich_calendar_observations``
    which matches by date + attendee email overlap. Unmatched Granola
    docs (meetings not on this user's calendar) are emitted as standalone
    observations so nothing is lost.
    """
    from deja.observations.calendar import fetch_calendar_backfill
    from deja.observations.granola import enrich_calendar_observations
    from deja.onboarding.runner import run_step

    def _fetch() -> list:
        cal_obs = fetch_calendar_backfill(days=days)
        return enrich_calendar_observations(cal_obs, days=days)

    return await run_step(
        name="calendar_backfill",
        fetch_fn=_fetch,
        wiki_lock=wiki_lock,
        gemini=gemini,
        force=force,
        on_progress=on_progress,
    )


# ---------------------------------------------------------------------------
# Step: Google Meet transcript backfill
# ---------------------------------------------------------------------------


async def backfill_meet_transcripts(
    *,
    days: int = 30,
    wiki_lock: asyncio.Lock | None = None,
    gemini: Any = None,
    force: bool = False,
    on_progress: Any = None,
) -> dict[str, Any]:
    """Ingest Google Meet transcripts from the last ``days`` days."""
    from deja.observations.meet import fetch_meet_transcripts_backfill
    from deja.onboarding.runner import run_step

    return await run_step(
        name="meet_transcript_backfill",
        fetch_fn=lambda: fetch_meet_transcripts_backfill(days=days),
        wiki_lock=wiki_lock,
        gemini=gemini,
        force=force,
        on_progress=on_progress,
    )


STEP_DISPATCH: dict[str, Any] = {
    "sent_email_backfill": backfill_sent_email,
    "imessage_backfill": backfill_imessage,
    "whatsapp_backfill": backfill_whatsapp,
    "calendar_backfill": backfill_calendar,
    "meet_transcript_backfill": backfill_meet_transcripts,
}


async def run_all_pending_steps(
    *,
    days: int = 30,
    wiki_lock: asyncio.Lock | None = None,
    gemini: Any = None,
    force: bool = False,
    only: str | None = None,
    on_progress: Any = None,
) -> list[dict[str, Any]]:
    """Run every onboarding step whose marker is not yet set.

    Steps run sequentially so they share the wiki lock cleanly and so
    later steps see pages created by earlier steps in their
    ``wiki_text`` context (iMessage enriches email-derived pages
    instead of duplicating them).

    ``only`` restricts to a single step name, useful for
    ``deja onboard --only imessage`` after granting Full Disk
    Access. ``force`` re-runs even completed steps.
    """
    # Share a lock across all steps so every wiki write in this run is
    # serialized against every other.
    if wiki_lock is None:
        wiki_lock = asyncio.Lock()

    only_map = {
        "email": "sent_email_backfill",
        "sent_email": "sent_email_backfill",
        "sent_email_backfill": "sent_email_backfill",
        "imessage": "imessage_backfill",
        "imessage_backfill": "imessage_backfill",
        "whatsapp": "whatsapp_backfill",
        "whatsapp_backfill": "whatsapp_backfill",
        "calendar": "calendar_backfill",
        "calendar_backfill": "calendar_backfill",
        "granola": "calendar_backfill",
        "meet": "meet_transcript_backfill",
        "meet_transcript": "meet_transcript_backfill",
        "meet_transcript_backfill": "meet_transcript_backfill",
    }
    only_step: str | None = None
    if only is not None:
        only_step = only_map.get(only.lower())
        if only_step is None:
            raise ValueError(
                f"Unknown onboarding step '{only}'. "
                f"Valid: {sorted(set(only_map))}"
            )

    summaries: list[dict[str, Any]] = []
    for step_name, _desc in ALL_STEPS:
        if only_step is not None and step_name != only_step:
            continue
        fn = STEP_DISPATCH[step_name]
        summary = await fn(
            days=days,
            wiki_lock=wiki_lock,
            gemini=gemini,
            force=force,
            on_progress=on_progress,
        )
        summaries.append(summary)
    return summaries
