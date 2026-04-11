"""GET /api/status — liveness probe.
GET /api/activity — recent activity feed for the notch popover."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from fastapi import APIRouter

from deja.web.helpers import OBSERVATIONS_LOG, INTEGRATIONS_LOG

router = APIRouter()


@router.get("/api/status")
def get_status() -> dict:
    """Return liveness info. ``monitor_running`` is true if a signal landed in
    the last 120s."""
    last_signal_time = None
    if OBSERVATIONS_LOG.exists():
        with open(OBSERVATIONS_LOG, "rb") as f:
            try:
                f.seek(-4096, 2)
            except OSError:
                f.seek(0)
            tail = f.read().decode("utf-8", errors="replace").splitlines()
        for line in reversed(tail):
            line = line.strip()
            if not line:
                continue
            try:
                last_signal_time = json.loads(line).get("timestamp")
                break
            except json.JSONDecodeError:
                continue

    last_analysis_time = None
    if INTEGRATIONS_LOG.exists():
        with open(INTEGRATIONS_LOG, "rb") as f:
            try:
                f.seek(-4096, 2)
            except OSError:
                f.seek(0)
            tail = f.read().decode("utf-8", errors="replace").splitlines()
        for line in reversed(tail):
            line = line.strip()
            if not line:
                continue
            try:
                last_analysis_time = json.loads(line).get("timestamp")
                break
            except json.JSONDecodeError:
                continue

    monitor_running = False
    if last_signal_time:
        last_dt = datetime.fromisoformat(last_signal_time.replace("Z", "+00:00"))
        if last_dt.tzinfo is None:
            last_dt = last_dt.astimezone()
        age = (datetime.now(timezone.utc) - last_dt).total_seconds()
        monitor_running = age < 120

    # Screen Recording permission check
    screen_recording = True
    try:
        from deja.observations.screenshot import screen_recording_granted

        screen_recording = screen_recording_granted()
    except Exception:
        pass

    return {
        "monitor_running": monitor_running,
        "last_signal_time": last_signal_time,
        "last_analysis_time": last_analysis_time,
        "screen_recording": screen_recording,
    }


_ACTIVITY_LINE_RE = re.compile(
    r"^- \*\*\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2})\]\*\*\s+"
    r"(?P<kind>[^\s—-]+)\s+[—-]\s*(?P<summary>.*)$"
)


@router.get("/api/activity")
def get_activity(limit: int = 50) -> dict:
    """Return the most recent activity entries from the wiki log.md.

    The activity feed is the notch popover's primary surface: it
    shows what the agent has been doing (dedup merges, integrate
    writes, commands, meetings). Each row parses one bullet from
    ``~/Deja/log.md`` via the stable `- **[ts]** kind — summary`
    format emitted by ``activity_log.append_log_entry``.
    """
    from deja.activity_log import LOG_PATH

    entries: list[dict] = []
    if LOG_PATH.exists():
        try:
            text = LOG_PATH.read_text(encoding="utf-8")
        except Exception:
            text = ""
        for line in text.splitlines():
            m = _ACTIVITY_LINE_RE.match(line.rstrip())
            if not m:
                continue
            entries.append(
                {
                    "timestamp": m.group("ts"),
                    "kind": m.group("kind"),
                    "summary": m.group("summary").strip(),
                }
            )

    # Newest first, capped at `limit`
    entries.reverse()
    if limit and limit > 0:
        entries = entries[:limit]
    return {"entries": entries}
