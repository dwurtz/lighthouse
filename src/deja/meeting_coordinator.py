"""Meeting coordinator — calendar-aware recording lifecycle.

Polls Google Calendar for active/imminent meetings with attendees.
When one is found, writes a prompt file that the Swift menu-bar app
reads to show a "Record this meeting?" banner. The user clicks Record
in the popover, Swift starts ScreenCaptureKit audio capture, and the
coordinator handles auto-stop on prolonged silence (detected by Swift)
and post-meeting processing.

Runs as a background task alongside the signal and analysis loops.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from deja.config import DEJA_HOME

log = logging.getLogger(__name__)

MEETING_PROMPT_PATH = DEJA_HOME / "meeting_prompt.json"
MEETING_STATE_PATH = DEJA_HOME / "meeting_state.json"


def get_active_or_imminent_meeting(lookahead_minutes: int = 1) -> dict | None:
    """Return the current or about-to-start meeting with attendees, or None.

    'Active' = start <= now <= end, has attendees, not all-day.
    'Imminent' = starts within lookahead_minutes.

    Returns the raw Google Calendar event dict with parsed fields.
    """
    from deja.observations.calendar import (
        _run_calendar_query,
        _attendee_names_and_emails,
        _is_real_meeting,
        _parse_event_time,
    )

    now = datetime.now(timezone.utc)
    # Look back 2 hours (for meetings in progress) and ahead
    events = _run_calendar_query(
        now - timedelta(hours=2),
        now + timedelta(minutes=lookahead_minutes),
        max_results=10,
    )

    for event in events:
        if not _is_real_meeting(event):
            continue

        start = _parse_event_time(event, "start")
        end = _parse_event_time(event, "end")
        if not start or not end:
            continue

        # Convert to aware for comparison
        now_local = datetime.now()
        start_naive = start
        end_naive = end

        # Is this meeting active or imminent?
        is_active = start_naive <= now_local <= end_naive
        is_imminent = (
            now_local < start_naive
            and (start_naive - now_local).total_seconds() <= lookahead_minutes * 60
        )

        if not is_active and not is_imminent:
            continue

        attendees = _attendee_names_and_emails(event)
        if not attendees:
            continue

        return {
            "event_id": event.get("id", ""),
            "title": event.get("summary", "(no title)"),
            "attendees": [
                {"name": name, "email": email}
                for name, email in attendees
            ],
            "start": start.isoformat(),
            "end": end.isoformat(),
            "video_link": _extract_video_link(event),
            "status": "active" if is_active else "imminent",
        }

    return None


def _extract_video_link(event: dict) -> str:
    """Extract video conference link from a calendar event."""
    conf = event.get("conferenceData", {})
    for ep in conf.get("entryPoints", []):
        if ep.get("entryPointType") == "video":
            return ep.get("uri", "")
    return ""


def write_meeting_prompt(meeting: dict) -> None:
    """Write the meeting prompt file for Swift to read.

    Swift polls this file every 3s. When it appears, the popover
    shows a "Record [title]?" banner.
    """
    MEETING_PROMPT_PATH.parent.mkdir(parents=True, exist_ok=True)
    MEETING_PROMPT_PATH.write_text(json.dumps(meeting, indent=2))
    log.info("Meeting prompt written: %s", meeting.get("title"))


def clear_meeting_prompt() -> None:
    """Remove the prompt file (Swift deletes it after user responds)."""
    try:
        MEETING_PROMPT_PATH.unlink(missing_ok=True)
    except OSError:
        pass


def read_meeting_state() -> dict | None:
    """Read the shared meeting state file."""
    try:
        if MEETING_STATE_PATH.exists():
            return json.loads(MEETING_STATE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        pass
    return None


def write_meeting_state(state: dict) -> None:
    """Write the shared meeting state file."""
    MEETING_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    MEETING_STATE_PATH.write_text(json.dumps(state, indent=2))


def clear_meeting_state() -> None:
    """Clear meeting state."""
    try:
        MEETING_STATE_PATH.unlink(missing_ok=True)
    except OSError:
        pass


async def meeting_poll_loop() -> None:
    """Background loop: detect recordable meetings and surface prompts.

    Runs every 30 seconds. When a meeting with attendees is active or
    about to start, writes a prompt file. The Swift app polls this file
    and shows the recording banner.

    Does NOT auto-start recording — the user must click Record.
    Does NOT auto-stop — silence detection in Swift handles that.
    """
    import asyncio

    last_prompted_event_id: str | None = None

    while True:
        try:
            state = read_meeting_state()
            is_recording = state and state.get("recording", False)

            if not is_recording:
                # Check for active/imminent meetings
                meeting = get_active_or_imminent_meeting(lookahead_minutes=5)
                if meeting:
                    event_id = meeting["event_id"]
                    # Don't re-prompt for the same meeting the user already dismissed
                    if event_id != last_prompted_event_id:
                        # Only write if prompt file doesn't already exist
                        if not MEETING_PROMPT_PATH.exists():
                            write_meeting_prompt(meeting)
                elif MEETING_PROMPT_PATH.exists():
                    # No active meeting — clear stale prompt
                    clear_meeting_prompt()
                    last_prompted_event_id = None
            else:
                # Recording is active — clear prompt file
                if MEETING_PROMPT_PATH.exists():
                    clear_meeting_prompt()

        except Exception:
            log.exception("Meeting poll cycle error")

        await asyncio.sleep(30)
