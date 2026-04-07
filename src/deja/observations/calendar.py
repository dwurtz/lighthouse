"""Calendar signal collector using the gws CLI tool.

Two modes:

  * ``collect_upcoming_events`` — steady-state, forward-looking (next
    2 hours). Gives the monitor loop heads-up signals about what's
    coming so the LLM can prep context.
  * ``collect_past_events`` — backward-looking (last N minutes). Tells
    the monitor loop what meetings just finished so the LLM can update
    project/people pages with "this meeting happened."
  * ``fetch_calendar_backfill`` — onboarding, last N days. The source
    of truth for *every meeting that happened* — attendees, duration,
    title. Granola notes are joined onto these events as enrichment,
    not as a parallel source.
"""

from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime, timedelta, timezone

from deja.observations.types import Observation

log = logging.getLogger(__name__)


def _run_calendar_query(
    time_min: datetime,
    time_max: datetime,
    max_results: int = 50,
) -> list[dict]:
    """Run a gws calendar events list query and return raw event dicts."""
    try:
        result = subprocess.run(
            [
                "gws", "calendar", "events", "list",
                "--params", json.dumps({
                    "calendarId": "primary",
                    "timeMin": time_min.isoformat(),
                    "timeMax": time_max.isoformat(),
                    "singleEvents": True,
                    "orderBy": "startTime",
                    "maxResults": max_results,
                }),
                "--format", "json",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            log.warning("gws calendar list failed: %s", result.stderr[:200])
            return []
        data = json.loads(result.stdout)
        return data.get("items", []) or []
    except subprocess.TimeoutExpired:
        log.warning("gws calendar list timed out")
        return []
    except json.JSONDecodeError:
        log.warning("gws calendar list returned invalid JSON")
        return []
    except FileNotFoundError:
        log.warning("gws CLI not found on PATH")
        return []
    except Exception:
        log.exception("Calendar query error")
        return []


def _parse_event_time(event: dict, key: str) -> datetime | None:
    """Parse event start or end time to naive local datetime."""
    block = event.get(key, {})
    raw = block.get("dateTime") or block.get("date") or ""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)
        return dt
    except (ValueError, TypeError):
        return None


def _attendee_names_and_emails(event: dict) -> list[tuple[str, str]]:
    """Return [(display_name, email), ...] for all attendees, skipping
    the organizer (self)."""
    attendees = event.get("attendees", []) or []
    organizer_email = (event.get("organizer") or {}).get("email", "").lower()
    creator_email = (event.get("creator") or {}).get("email", "").lower()
    self_emails = {organizer_email, creator_email} - {""}

    results: list[tuple[str, str]] = []
    for a in attendees:
        email = (a.get("email") or "").lower()
        if a.get("self") or email in self_emails:
            continue
        name = a.get("displayName") or email
        results.append((name, email))
    return results


def _is_real_meeting(event: dict) -> bool:
    """Filter out all-day events, cancelled events, and events with no
    other attendees (blocks, focus time, reminders)."""
    if event.get("status") == "cancelled":
        return False
    start = event.get("start", {})
    if "date" in start and "dateTime" not in start:
        return False  # all-day event
    attendees = _attendee_names_and_emails(event)
    if not attendees:
        return False  # solo block
    return True


def _build_event_observation(
    event: dict,
    direction: str = "upcoming",
) -> Observation | None:
    """Build one Observation from a calendar event."""
    event_id = event.get("id")
    if not event_id:
        return None

    summary = event.get("summary", "(no title)")
    start = _parse_event_time(event, "start")
    end = _parse_event_time(event, "end")
    if not start:
        return None

    attendees = _attendee_names_and_emails(event)
    attendee_display = ", ".join(
        f"{name} ({email})" if name != email else email
        for name, email in attendees[:8]
    )

    # Duration
    duration_str = ""
    if start and end and end > start:
        mins = int((end - start).total_seconds() / 60)
        if mins > 0:
            duration_str = f" ({mins} min)"

    if direction == "past":
        text = f"Meeting happened: {summary}{duration_str}"
        id_prefix = "calendar-past"
    else:
        time_display = start.strftime("%H:%M")
        text = f"Upcoming meeting: {summary} at {time_display}{duration_str}"
        id_prefix = "calendar"

    if attendee_display:
        text += f"\nAttendees: {attendee_display}"

    # Include conference link type if present (Meet, Zoom, etc.)
    conf = event.get("conferenceData", {})
    if conf:
        entry_points = conf.get("entryPoints", [])
        for ep in entry_points:
            if ep.get("entryPointType") == "video":
                text += f"\nVideo: {ep.get('uri', '')[:80]}"
                break

    # Location
    location = event.get("location", "")
    if location:
        text += f"\nLocation: {location[:100]}"

    sender = f"Meeting: {summary}"
    if attendees:
        names = [name for name, _ in attendees[:3]]
        sender = f"Meeting with {', '.join(names)}"

    return Observation(
        source="calendar",
        sender=sender[:100],
        text=text[:1000],
        timestamp=start,
        id_key=f"{id_prefix}-{event_id}",
    )


# ---------------------------------------------------------------------------
# Steady-state collectors
# ---------------------------------------------------------------------------


def collect_upcoming_events(hours_ahead: int = 2) -> list[Observation]:
    """Collect upcoming calendar events (next N hours)."""
    now = datetime.now(timezone.utc)
    events = _run_calendar_query(now, now + timedelta(hours=hours_ahead), max_results=5)
    results: list[Observation] = []
    for event in events:
        if not _is_real_meeting(event):
            continue
        obs = _build_event_observation(event, direction="upcoming")
        if obs:
            results.append(obs)
    return results


def collect_past_events(lookback_minutes: int = 120) -> list[Observation]:
    """Collect meetings that ended in the last N minutes.

    This is the backward-looking complement to ``collect_upcoming_events``.
    Together they give the monitor loop a complete picture: what's coming
    up AND what just happened. The "just happened" signal is what lets
    the integrate cycle update project pages from "scheduled" to "met."
    """
    now = datetime.now(timezone.utc)
    events = _run_calendar_query(
        now - timedelta(minutes=lookback_minutes),
        now,
        max_results=10,
    )
    results: list[Observation] = []
    for event in events:
        if not _is_real_meeting(event):
            continue
        end = _parse_event_time(event, "end")
        if not end:
            continue
        # Only emit if the meeting has actually ended
        if end.replace(tzinfo=None) > datetime.now():
            continue
        obs = _build_event_observation(event, direction="past")
        if obs:
            results.append(obs)
    return results


# ---------------------------------------------------------------------------
# Onboarding backfill
# ---------------------------------------------------------------------------


def fetch_calendar_backfill(days: int = 30, max_results: int = 500) -> list[Observation]:
    """Return one Observation per real meeting in the last ``days`` days.

    This is the source of truth for "what meetings happened." Every
    meeting with at least one other attendee is emitted, regardless of
    whether Granola captured notes for it. Granola enrichment happens
    separately — the calendar event is the authoritative record that
    the meeting occurred.

    Paginates via multiple queries if needed (Calendar API caps at 2500
    per query, but we default to 500 which covers most users).
    """
    now = datetime.now(timezone.utc)
    time_min = now - timedelta(days=days)

    events = _run_calendar_query(time_min, now, max_results=max_results)
    log.info("Calendar backfill: %d raw events in last %d days", len(events), days)

    results: list[Observation] = []
    for event in events:
        if not _is_real_meeting(event):
            continue
        obs = _build_event_observation(event, direction="past")
        if obs:
            results.append(obs)

    results.sort(key=lambda o: o.timestamp)
    log.info("Calendar backfill: %d real meetings after filtering", len(results))
    return results
