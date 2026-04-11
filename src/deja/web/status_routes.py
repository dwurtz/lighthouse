"""GET /api/status — liveness probe.
GET /api/activity — recent activity feed for the notch popover."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter

from deja import audit
from deja.web.helpers import OBSERVATIONS_LOG

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
    if audit.AUDIT_LOG.exists():
        with open(audit.AUDIT_LOG, "rb") as f:
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
                last_analysis_time = json.loads(line).get("ts")
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


@router.get("/api/activity")
def get_activity(limit: int = 50) -> dict:
    """Return the most recent audit entries for the notch Activity tab.

    Reads ``~/.deja/audit.jsonl`` — one line per discrete agent action
    — and projects it into the ``{timestamp, kind, summary}`` shape the
    Swift notch UI expects. ``kind`` is the audit ``action`` field
    (wiki_write, reminder_resolve, etc.) and ``summary`` is derived
    from ``target`` + ``reason``.
    """
    raw = audit.read_recent(limit=limit)
    entries: list[dict] = []
    for e in raw:
        ts_iso = e.get("ts", "")
        # Convert ISO8601 to "YYYY-MM-DD HH:MM" local for UI rendering.
        ts_short = ts_iso[:16].replace("T", " ") if ts_iso else ""
        action = e.get("action", "")
        target = e.get("target", "")
        reason = e.get("reason", "")
        summary = f"{target} — {reason}" if target else reason
        entries.append(
            {
                "timestamp": ts_short,
                "kind": action,
                "summary": summary,
            }
        )
    return {"entries": entries}
