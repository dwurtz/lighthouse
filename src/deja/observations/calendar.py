"""Calendar signal collector using the gws CLI tool.

Steady-state uses the Calendar incremental-sync API
(events.list with ``syncToken``) keyed off a per-calendar cursor so
each cycle only pulls events that were created, updated, or cancelled
since the last poll.

  * ``CalendarObserver`` — delta via syncToken
  * ``fetch_calendar_backfill`` — onboarding only (last N days, snapshot)

Cursor persistence: ``~/.deja/calendar_sync_tokens.json`` — a JSON map
``{calendarId: syncToken}``. On first run for a calendar we do a bounded
initial sync (``timeMin`` = now - 1h, ``timeMax`` = now + 2h) which
returns a ``nextSyncToken``; we store that and then use it for all
subsequent deltas. When a syncToken expires (410 / FULL_SYNC_REQUIRED)
we drop it and re-bootstrap.

No silent fallback to snapshot polling — on failure we log.error and
return [].
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from deja.config import DEJA_HOME
from deja.observations.base import BaseObserver
from deja.observations.types import Observation

log = logging.getLogger(__name__)


_TOKENS_PATH = DEJA_HOME / "calendar_sync_tokens.json"
# Calendars we track. The primary calendar is the only one the rest of
# the agent reasons about today; adding more is a matter of extending
# this list (each gets its own cursor).
_CALENDARS = ["primary"]


# ---------------------------------------------------------------------------
# Cursor helpers
# ---------------------------------------------------------------------------


def _read_tokens() -> dict[str, str]:
    try:
        if not _TOKENS_PATH.exists():
            return {}
        data = json.loads(_TOKENS_PATH.read_text())
        if not isinstance(data, dict):
            return {}
        # String-only values; guard against garbage
        return {str(k): str(v) for k, v in data.items() if isinstance(v, str)}
    except (OSError, json.JSONDecodeError):
        log.warning("calendar_sync_tokens.json unreadable; will re-bootstrap")
        return {}


def _write_tokens_atomic(tokens: dict[str, str]) -> None:
    try:
        DEJA_HOME.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".calendar_tokens.", dir=str(DEJA_HOME))
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(tokens, f)
            os.replace(tmp, _TOKENS_PATH)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception:
        log.exception("Failed to persist calendar sync tokens")


# ---------------------------------------------------------------------------
# Event helpers (shared with backfill)
# ---------------------------------------------------------------------------


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
    """Return [(display_name, email), ...] for all attendees, skipping self."""
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
    """Filter out all-day events, cancelled events, and solo blocks."""
    if event.get("status") == "cancelled":
        return False
    start = event.get("start", {})
    if "date" in start and "dateTime" not in start:
        return False
    attendees = _attendee_names_and_emails(event)
    if not attendees:
        return False
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

    duration_str = ""
    if start and end and end > start:
        mins = int((end - start).total_seconds() / 60)
        if mins > 0:
            duration_str = f" ({mins} min)"

    if direction == "past":
        text = f"Meeting happened: {summary}{duration_str}"
        id_prefix = "calendar-past"
    elif direction == "changed":
        time_display = start.strftime("%Y-%m-%d %H:%M")
        text = f"Meeting updated: {summary} at {time_display}{duration_str}"
        id_prefix = "calendar-changed"
    else:
        time_display = start.strftime("%H:%M")
        text = f"Upcoming meeting: {summary} at {time_display}{duration_str}"
        id_prefix = "calendar"

    if attendee_display:
        text += f"\nAttendees: {attendee_display}"

    conf = event.get("conferenceData", {})
    if conf:
        entry_points = conf.get("entryPoints", [])
        for ep in entry_points:
            if ep.get("entryPointType") == "video":
                text += f"\nVideo: {ep.get('uri', '')[:80]}"
                break

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
# Sync-token delta fetch
# ---------------------------------------------------------------------------


def _run_events_list(params: dict) -> tuple[dict | None, str | None]:
    """Run ``gws calendar events list``. Returns (data, stderr)."""
    try:
        result = subprocess.run(
            [
                "gws", "calendar", "events", "list",
                "--params", json.dumps(params),
                "--format", "json",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        log.error("gws calendar events list timed out")
        return None, "timeout"
    except FileNotFoundError:
        log.error("gws CLI not found on PATH")
        return None, "missing-cli"

    if result.returncode != 0:
        return None, result.stderr or ""
    try:
        return json.loads(result.stdout), None
    except json.JSONDecodeError:
        log.error("gws calendar events list returned invalid JSON")
        return None, "invalid-json"


def _bootstrap_sync_token(calendar_id: str) -> str | None:
    """Do the initial sync for ``calendar_id``; return nextSyncToken.

    Uses a tight window (now-1h .. now+2h) so we don't pull the whole
    calendar. The events returned here are discarded — only the token
    is retained. Historical events are onboarding's concern.
    """
    now = datetime.now(timezone.utc)
    time_min = (now - timedelta(hours=1)).isoformat()
    time_max = (now + timedelta(hours=2)).isoformat()

    page_token: str | None = None
    sync_token: str | None = None
    for _ in range(20):
        params: dict = {
            "calendarId": calendar_id,
            "timeMin": time_min,
            "timeMax": time_max,
            "singleEvents": True,
            "showDeleted": True,
            "maxResults": 250,
        }
        if page_token:
            params["pageToken"] = page_token
        data, err = _run_events_list(params)
        if data is None:
            log.error("Calendar bootstrap failed for %s: %s", calendar_id, (err or "")[:200])
            return None
        page_token = data.get("nextPageToken")
        if not page_token:
            sync_token = data.get("nextSyncToken")
            break
    if sync_token:
        log.info("Calendar %s sync token bootstrapped", calendar_id)
    return sync_token


def _list_changes(calendar_id: str, sync_token: str) -> tuple[list[dict], str | None, bool]:
    """Page through changes since ``sync_token``.

    Returns (events, new_sync_token, expired). ``expired`` is True when
    the token is no longer valid (410 / FULL_SYNC_REQUIRED) — caller
    must drop the token and re-bootstrap.
    """
    events: list[dict] = []
    page_token: str | None = None
    new_sync_token: str | None = None

    for _ in range(20):
        params: dict = {
            "calendarId": calendar_id,
            "syncToken": sync_token,
            "showDeleted": True,
            "maxResults": 250,
        }
        if page_token:
            params["pageToken"] = page_token
        data, err = _run_events_list(params)
        if data is None:
            stderr = (err or "").lower()
            if "410" in stderr or "fullsyncrequired" in stderr or "sync token" in stderr:
                return [], None, True
            log.error("Calendar delta failed for %s: %s", calendar_id, (err or "")[:200])
            return [], None, False
        events.extend(data.get("items", []) or [])
        page_token = data.get("nextPageToken")
        if not page_token:
            new_sync_token = data.get("nextSyncToken")
            break

    return events, new_sync_token, False


def _classify_event_direction(event: dict) -> str:
    """Decide whether a changed event is upcoming / past / changed."""
    start = _parse_event_time(event, "start")
    end = _parse_event_time(event, "end")
    now_naive = datetime.now()
    if end and end <= now_naive:
        return "past"
    if start and start > now_naive:
        return "upcoming"
    return "changed"


# ---------------------------------------------------------------------------
# Observer
# ---------------------------------------------------------------------------


class CalendarObserver(BaseObserver):
    """Delta-based calendar collector via events.list syncToken."""

    def __init__(self, hours_ahead: int = 2, lookback_minutes: int = 120) -> None:
        # Retained for API compat; unused in delta mode.
        self.hours_ahead = hours_ahead
        self.lookback_minutes = lookback_minutes

    @property
    def name(self) -> str:
        return "Calendar"

    def collect(self) -> list[Observation]:
        tokens = _read_tokens()
        observations: list[Observation] = []
        dirty = False

        for calendar_id in _CALENDARS:
            sync_token = tokens.get(calendar_id)
            if not sync_token:
                new_token = _bootstrap_sync_token(calendar_id)
                if new_token:
                    tokens[calendar_id] = new_token
                    dirty = True
                else:
                    log.error("Calendar %s: bootstrap failed, skipping", calendar_id)
                # Don't emit observations on bootstrap cycle — that would
                # double up with onboarding backfill.
                continue

            events, new_token, expired = _list_changes(calendar_id, sync_token)
            if expired:
                log.warning("Calendar %s sync token expired; re-bootstrapping", calendar_id)
                replacement = _bootstrap_sync_token(calendar_id)
                if replacement:
                    tokens[calendar_id] = replacement
                else:
                    tokens.pop(calendar_id, None)
                dirty = True
                continue

            if new_token and new_token != sync_token:
                tokens[calendar_id] = new_token
                dirty = True

            for event in events:
                if not _is_real_meeting(event):
                    continue
                direction = _classify_event_direction(event)
                obs = _build_event_observation(event, direction=direction)
                if obs:
                    observations.append(obs)

        if dirty:
            _write_tokens_atomic(tokens)

        return observations


# ---------------------------------------------------------------------------
# Onboarding backfill (snapshot, unchanged behaviour)
# ---------------------------------------------------------------------------


def _run_calendar_query(
    time_min: datetime,
    time_max: datetime,
    max_results: int = 50,
) -> list[dict]:
    """Run a gws calendar events list query and return raw event dicts."""
    data, err = _run_events_list({
        "calendarId": "primary",
        "timeMin": time_min.isoformat(),
        "timeMax": time_max.isoformat(),
        "singleEvents": True,
        "orderBy": "startTime",
        "maxResults": max_results,
    })
    if data is None:
        if err:
            log.warning("gws calendar list failed: %s", err[:200])
        return []
    return data.get("items", []) or []


def collect_upcoming_events(hours_ahead: int = 2) -> list[Observation]:
    """Snapshot helper — retained for any caller (e.g. ad-hoc CLI).

    Not used by the steady-state observer anymore (delta covers it)."""
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
    """Snapshot helper — retained for any caller. Not used by the delta observer."""
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
        if end.replace(tzinfo=None) > datetime.now():
            continue
        obs = _build_event_observation(event, direction="past")
        if obs:
            results.append(obs)
    return results


def fetch_calendar_backfill(days: int = 30, max_results: int = 500) -> list[Observation]:
    """Return one Observation per real meeting in the last ``days`` days.

    Onboarding source of truth. Delta collector handles steady-state.
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
