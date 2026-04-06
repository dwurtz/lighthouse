"""Goal action executor — real-world operations the agent performs autonomously.

When goals.md defines an automation ("When a TeamSnap email arrives,
create a calendar event"), the integrate or reflect cycle can emit
structured ``goal_actions`` alongside wiki updates. This module
executes those actions via the appropriate tool (gws CLI, macOS
notifications, etc.).

Safety model:
  - Actions only fire when goals.md explicitly defines the automation.
    The LLM prompt includes goals.md and is instructed to only emit
    actions that match a user-defined goal.
  - ``draft_email`` creates DRAFTS, never sends. The user reviews in
    Gmail before sending.
  - Calendar and task operations are self-addressed (the user's own
    account). No external effects without explicit send.
  - ``notify`` is read-only (macOS notification banner).
  - Every action is logged to log.md for Obsidian visibility.

Supported action types:
  - calendar_create   — create a Google Calendar event
  - calendar_update   — update an existing event by ID
  - draft_email       — create a Gmail draft (NOT send)
  - create_task       — add to Google Tasks
  - complete_task     — mark a task done by ID
  - notify            — macOS notification banner
"""

from __future__ import annotations

import json
import logging
import subprocess
from typing import Any

log = logging.getLogger(__name__)


def execute_action(action: dict) -> bool:
    """Execute one goal_action dict. Returns True on success.

    Each action has {type, params, reason}. Unknown types are logged
    and skipped — never raise, so one bad action doesn't block others.
    """
    action_type = action.get("type", "")
    params = action.get("params") or {}
    reason = action.get("reason", "")

    executor = _EXECUTORS.get(action_type)
    if not executor:
        log.info("goal_action: unknown type '%s' — skipping", action_type)
        return False

    try:
        executor(params, reason)
        return True
    except Exception:
        log.exception("goal_action '%s' failed", action_type)
        return False


def execute_all(actions: list[dict]) -> int:
    """Execute a list of goal_actions. Returns count of successful ones."""
    executed = 0
    for a in actions:
        if execute_action(a):
            executed += 1
    if executed:
        log.info("goal_actions: executed %d/%d", executed, len(actions))
    return executed


# ---------------------------------------------------------------------------
# Individual action executors
# ---------------------------------------------------------------------------

def _gws_run(service_args: list[str], timeout: int = 15) -> subprocess.CompletedProcess:
    """Run a gws CLI command. Raises on timeout."""
    return subprocess.run(
        ["gws"] + service_args,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _log_action(action_type: str, summary: str) -> None:
    """Write a human-readable entry to log.md."""
    try:
        from lighthouse.activity_log import append_log_entry
        append_log_entry("action", f"{action_type}: {summary}")
    except Exception:
        pass


def _calendar_create(params: dict, reason: str) -> None:
    """Create a Google Calendar event."""
    summary = params.get("summary", "")
    start = params.get("start", "")
    end = params.get("end", "")
    if not summary or not start or not end:
        log.warning("calendar_create: missing summary/start/end")
        return

    event_body: dict = {
        "summary": summary,
        "start": {"dateTime": start},
        "end": {"dateTime": end},
    }
    if params.get("location"):
        event_body["location"] = params["location"]
    if params.get("description"):
        event_body["description"] = params["description"]

    r = _gws_run([
        "calendar", "events", "insert",
        "--params", json.dumps({"calendarId": "primary"}),
        "--json", json.dumps(event_body),
    ])
    if r.returncode == 0:
        log.info("calendar_create: '%s' at %s — %s", summary, start, reason)
        _log_action("calendar_create", f"{summary} at {start}")
    else:
        log.warning("calendar_create failed: %s", r.stderr[:200])


def _calendar_update(params: dict, reason: str) -> None:
    """Update an existing Google Calendar event by ID."""
    event_id = params.get("event_id", "")
    if not event_id:
        log.warning("calendar_update: missing event_id")
        return

    update_body: dict = {}
    for key in ("summary", "location", "description"):
        if params.get(key):
            update_body[key] = params[key]
    if params.get("start"):
        update_body["start"] = {"dateTime": params["start"]}
    if params.get("end"):
        update_body["end"] = {"dateTime": params["end"]}

    if not update_body:
        log.warning("calendar_update: nothing to update")
        return

    r = _gws_run([
        "calendar", "events", "patch",
        "--params", json.dumps({"calendarId": "primary", "eventId": event_id}),
        "--json", json.dumps(update_body),
    ])
    if r.returncode == 0:
        log.info("calendar_update: %s — %s", event_id, reason)
        _log_action("calendar_update", f"updated event {event_id}")
    else:
        log.warning("calendar_update failed: %s", r.stderr[:200])


def _draft_email(params: dict, reason: str) -> None:
    """Create a Gmail draft (does NOT send — user reviews in Gmail first)."""
    import base64

    to = params.get("to", "")
    subject = params.get("subject", "")
    body = params.get("body", "")
    if not to or not subject:
        log.warning("draft_email: missing to/subject")
        return

    from lighthouse.identity import load_user
    user = load_user()
    from_addr = user.email or "me"

    raw_msg = f"From: {from_addr}\nTo: {to}\nSubject: {subject}\n\n{body}"
    encoded = base64.urlsafe_b64encode(raw_msg.encode()).decode()

    r = _gws_run([
        "gmail", "users", "drafts", "create",
        "--params", json.dumps({"userId": "me"}),
        "--json", json.dumps({"message": {"raw": encoded}}),
    ])
    if r.returncode == 0:
        log.info("draft_email: draft to %s re: '%s' — %s", to, subject, reason)
        _log_action("draft_email", f"draft to {to}: {subject}")
    else:
        log.warning("draft_email failed: %s", r.stderr[:200])


def _create_task(params: dict, reason: str) -> None:
    """Add a task to Google Tasks."""
    title = params.get("title", "")
    if not title:
        log.warning("create_task: missing title")
        return

    task_body: dict = {"title": title}
    if params.get("notes"):
        task_body["notes"] = params["notes"]
    if params.get("due"):
        task_body["due"] = params["due"]

    # Use the default task list
    tasklist = params.get("tasklist", "@default")
    r = _gws_run([
        "tasks", "tasks", "insert",
        "--params", json.dumps({"tasklist": tasklist}),
        "--json", json.dumps(task_body),
    ])
    if r.returncode == 0:
        log.info("create_task: '%s' — %s", title, reason)
        _log_action("create_task", title)
    else:
        log.warning("create_task failed: %s", r.stderr[:200])


def _complete_task(params: dict, reason: str) -> None:
    """Mark a Google Tasks task as completed."""
    task_id = params.get("task_id", "")
    tasklist = params.get("tasklist", "@default")
    if not task_id:
        log.warning("complete_task: missing task_id")
        return

    r = _gws_run([
        "tasks", "tasks", "patch",
        "--params", json.dumps({"tasklist": tasklist, "task": task_id}),
        "--json", json.dumps({"status": "completed"}),
    ])
    if r.returncode == 0:
        log.info("complete_task: %s — %s", task_id, reason)
        _log_action("complete_task", f"completed task {task_id}")
    else:
        log.warning("complete_task failed: %s", r.stderr[:200])


def _notify(params: dict, reason: str) -> None:
    """Show a macOS notification banner."""
    title = params.get("title", "Lighthouse")
    message = params.get("message", "")
    if not message:
        log.warning("notify: missing message")
        return

    script = f'display notification "{message}" with title "{title}"'
    subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        timeout=5,
    )
    log.info("notify: '%s' — %s", message[:80], reason)
    _log_action("notify", message[:80])


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_EXECUTORS = {
    "calendar_create": _calendar_create,
    "calendar_update": _calendar_update,
    "draft_email": _draft_email,
    "create_task": _create_task,
    "complete_task": _complete_task,
    "notify": _notify,
}
