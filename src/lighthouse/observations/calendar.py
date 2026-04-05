"""Calendar signal collector using the gws CLI tool."""

from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime, timedelta, timezone

from lighthouse.observations.types import Observation

log = logging.getLogger(__name__)


def collect_upcoming_events(hours_ahead: int = 2) -> list[Observation]:
    """Collect upcoming calendar events using gws CLI."""
    signals: list[Observation] = []

    now = datetime.now(timezone.utc)
    time_max = now + timedelta(hours=hours_ahead)

    try:
        result = subprocess.run(
            [
                "gws", "calendar", "events", "list",
                "--params", json.dumps({
                    "calendarId": "primary",
                    "timeMin": now.isoformat(),
                    "timeMax": time_max.isoformat(),
                    "singleEvents": True,
                    "orderBy": "startTime",
                    "maxResults": 5,
                }),
                "--format", "json",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )

        if result.returncode != 0:
            log.warning("gws calendar list failed: %s", result.stderr[:200])
            return signals

        data = json.loads(result.stdout)
        events = data.get("items", [])

        seen_ids: set[str] = set()

        for event in events:
            event_id = event.get("id")
            if not event_id or event_id in seen_ids:
                continue
            seen_ids.add(event_id)

            summary = event.get("summary", "(no title)")

            # Parse start time
            start = event.get("start", {})
            start_str = start.get("dateTime", start.get("date", ""))
            try:
                if "T" in start_str:
                    ts = datetime.fromisoformat(start_str)
                    time_display = ts.strftime("%H:%M")
                else:
                    time_display = start_str
                    ts = datetime.now()
            except (ValueError, TypeError):
                time_display = start_str
                ts = datetime.now()

            # Extract attendees
            attendees = event.get("attendees", [])
            attendee_names = []
            for a in attendees[:5]:
                name = a.get("displayName") or a.get("email", "")
                if name:
                    attendee_names.append(name)

            # Build text
            text = f"Meeting: {summary} at {time_display}"
            if attendee_names:
                text += f" with {', '.join(attendee_names)}"

            signals.append(Observation(
                source="calendar",
                sender="calendar",
                text=text[:500],
                timestamp=ts if ts.tzinfo is None else ts.replace(tzinfo=None),
                id_key=f"calendar-{event_id}",
            ))

    except subprocess.TimeoutExpired:
        log.warning("gws calendar list timed out")
    except json.JSONDecodeError:
        log.warning("gws calendar list returned invalid JSON")
    except FileNotFoundError:
        log.warning("gws CLI not found on PATH")
    except Exception:
        log.exception("Calendar collector error")

    return signals
