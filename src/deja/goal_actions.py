"""Goal action executor — real-world operations the agent performs autonomously.

When goals.md defines an automation ("When a TeamSnap email arrives,
create a calendar event"), the integrate or reflect cycle can emit
structured ``goal_actions`` alongside wiki updates. This module
executes those actions via the appropriate Google API.

Safety model:
  - Actions only fire when goals.md explicitly defines the automation.
    The LLM prompt includes goals.md and is instructed to only emit
    actions that match a user-defined goal.
  - ``draft_email`` creates DRAFTS, never sends. The user reviews in
    Gmail before sending.
  - Calendar and task operations are self-addressed (the user's own
    account). No external effects without explicit send.
  - ``notify`` is read-only (macOS notification banner).
  - Every action is recorded via ``audit.record()`` so it's grep-able
    in ``~/.deja/audit.jsonl``.

Transport: direct ``googleapiclient`` via ``deja.google_api.get_service``
— the same OAuth token that powers observation collectors. No gws CLI
dependency.

Supported action types:
  - calendar_create   — create a Google Calendar event
  - calendar_update   — update an existing event by ID
  - draft_email       — create a Gmail draft (NOT send)
  - create_task       — add to Google Tasks
  - complete_task     — mark a task done by ID
  - notify            — macOS notification banner
"""

from __future__ import annotations

import logging
from datetime import datetime

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
# Artifact tracking — for undo support on voice-command dispatch.
# ---------------------------------------------------------------------------
#
# When an executor creates a real-world artifact (calendar event, Gmail
# draft, Google Tasks row, goals.md line), it can call
# ``_record_artifact`` with a descriptor that the caller stores and uses
# later to undo the action. The sink is a contextvar so concurrent
# FastAPI requests don't clobber each other's artifacts.

from contextvars import ContextVar

_artifact_sink: ContextVar[list[dict] | None] = ContextVar(
    "_artifact_sink", default=None,
)


def _record_artifact(artifact: dict) -> None:
    """Record a created artifact in the current sink if one is active.

    No-op when called outside a ``collect_artifacts`` scope — existing
    callers (integrate cycles, cos subprocess) don't set a sink, so
    executors keep running unchanged.
    """
    sink = _artifact_sink.get()
    if sink is not None:
        sink.append(artifact)


def execute_with_artifacts(actions: list[dict]) -> tuple[int, list[dict]]:
    """Execute actions while capturing undo descriptors from each.

    Returns ``(executed_count, artifacts)``. Artifacts are in dispatch
    order; callers can hand them to an undo endpoint with a short TTL.
    """
    sink: list[dict] = []
    token = _artifact_sink.set(sink)
    try:
        count = execute_all(actions)
    finally:
        _artifact_sink.reset(token)
    return count, sink


# ---------------------------------------------------------------------------
# Service helpers
# ---------------------------------------------------------------------------

def _service(name: str, version: str):
    """Return a cached Google API service, or None on auth failure.

    Centralizes the "setup not complete / token unrecoverable" fallback
    so each executor can log-and-skip rather than tracing the error up
    into the agent loop.
    """
    try:
        from deja.google_api import get_service
        return get_service(name, version)
    except Exception:
        log.warning(
            "goal_action: %s/%s service unavailable — is setup complete?",
            name, version, exc_info=True,
        )
        return None


def _log_action(action_type: str, summary: str, reason: str = "") -> None:
    """Record one goal_action execution in the audit log."""
    try:
        from deja import audit
        audit.record(
            "goal_action",
            target=f"action/{action_type}",
            reason=f"{summary}" + (f" — {reason}" if reason else ""),
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Individual action executors
# ---------------------------------------------------------------------------

def _strip_deja_prefix(title: str) -> str:
    """Return title with any leading ``[Deja]`` / ``❓`` marker stripped."""
    t = (title or "").strip()
    if t.startswith("[Deja]"):
        t = t[len("[Deja]"):].strip()
    if t.startswith("❓"):
        t = t[len("❓"):].strip()
    return t


def _calendar_create(params: dict, reason: str) -> None:
    """Create a Google Calendar event, skipping if a similar one already exists.

    Params: ``summary``, ``start``, ``end``, optional ``location``,
    ``description``, and ``kind``. ``kind`` is one of:

    - ``"firm"`` (default): real meeting/appointment. No prefix. Google
      Calendar's default reminders apply (e.g. 10-min popup).
    - ``"reminder"``: time-bound nudge. Summary is prefixed with
      ``"[Deja] "`` and the event overrides reminders to a single popup
      at start time (minutes=0) so the user gets pinged AT time T.
    - ``"question"``: open question / soft suggestion. Summary prefixed
      with ``"[Deja] ❓ "``. Same popup-at-start override as reminder.

    Dedup: compares un-prefixed titles so a re-run with a different
    ``kind`` won't double-insert. Searches ±1h around ``start``.
    """
    summary = params.get("summary", "")
    start = params.get("start", "")
    end = params.get("end", "")
    kind = params.get("kind", "firm")
    if not summary or not start or not end:
        log.warning("calendar_create: missing summary/start/end")
        return

    if kind == "reminder" and not summary.startswith("[Deja]"):
        summary = f"[Deja] {summary}"
    elif kind == "question" and not summary.startswith("[Deja]"):
        summary = f"[Deja] ❓ {summary}"

    svc = _service("calendar", "v3")
    if svc is None:
        log.warning("calendar_create: skipped (no service)")
        return

    # Dedup: compare un-prefixed titles so re-runs that flip kind don't double-insert
    try:
        from datetime import timedelta
        start_dt = datetime.fromisoformat(start)
        search_min = (start_dt - timedelta(hours=1)).isoformat()
        search_max = (start_dt + timedelta(hours=1)).isoformat()

        existing = svc.events().list(
            calendarId="primary",
            timeMin=search_min,
            timeMax=search_max,
            singleEvents=True,
            maxResults=10,
        ).execute()
        new_title = _strip_deja_prefix(summary).lower()
        for event in existing.get("items", []):
            existing_title = _strip_deja_prefix(event.get("summary") or "").lower()
            if existing_title == new_title:
                log.info(
                    "calendar_create: skipping duplicate — '%s' already exists at %s",
                    summary, start,
                )
                return
    except Exception:
        log.debug("calendar_create dedup check failed, proceeding", exc_info=True)

    event_body: dict = {
        "summary": summary,
        "start": {"dateTime": start},
        "end": {"dateTime": end},
    }
    if params.get("location"):
        event_body["location"] = params["location"]
    if params.get("description"):
        event_body["description"] = params["description"]
    if kind in ("reminder", "question"):
        event_body["reminders"] = {
            "useDefault": False,
            "overrides": [{"method": "popup", "minutes": 0}],
        }

    try:
        created = svc.events().insert(
            calendarId="primary", body=event_body,
        ).execute()
    except Exception as e:
        log.warning("calendar_create failed: %s", type(e).__name__)
        return
    log.info("calendar_create: '%s' at %s — %s", summary, start, reason)
    _log_action("calendar_create", f"{summary} at {start}")
    event_id = (created or {}).get("id", "")
    if event_id:
        _record_artifact({
            "kind": "calendar_event",
            "id": event_id,
            "summary": summary,
        })


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

    svc = _service("calendar", "v3")
    if svc is None:
        log.warning("calendar_update: skipped (no service)")
        return

    try:
        svc.events().patch(
            calendarId="primary",
            eventId=event_id,
            body=update_body,
        ).execute()
    except Exception as e:
        log.warning("calendar_update failed: %s", type(e).__name__)
        return
    log.info("calendar_update: %s — %s", event_id, reason)
    _log_action("calendar_update", f"updated event {event_id}")


def _draft_email(params: dict, reason: str) -> None:
    """Create a Gmail draft (does NOT send — user reviews in Gmail first)."""
    import base64

    to = params.get("to", "")
    subject = params.get("subject", "")
    body = params.get("body", "")
    if not to or not subject:
        log.warning("draft_email: missing to/subject")
        return

    from deja.identity import load_user
    user = load_user()
    from_addr = user.email or "me"

    raw_msg = f"From: {from_addr}\nTo: {to}\nSubject: {subject}\n\n{body}"
    encoded = base64.urlsafe_b64encode(raw_msg.encode()).decode()

    svc = _service("gmail", "v1")
    if svc is None:
        log.warning("draft_email: skipped (no service)")
        return

    try:
        svc.users().drafts().create(
            userId="me",
            body={"message": {"raw": encoded}},
        ).execute()
    except Exception as e:
        log.warning("draft_email failed: %s", type(e).__name__)
        return
    log.info("draft_email: draft to %s re: '%s' — %s", to, subject, reason)
    _log_action("draft_email", f"draft to {to}: {subject}")


def _send_email_to_self(params: dict, reason: str) -> None:
    """Send an email to the user's own address. Used as a push channel.

    Unlike _draft_email (third-party, user reviews before send), this
    action sends immediately. Scope is restricted to the user's own
    email to prevent accidental outreach. Intended use: the chief-of-
    staff agent pinging the user with "here's what I noticed / did"
    summaries that are readable on mobile.

    Threading: pass ``in_reply_to`` (the original RFC822 Message-Id) and
    ``thread_id`` (Gmail's opaque thread id) when replying in-thread to
    a user message. Gmail needs BOTH — ``threadId`` on the send body
    keeps it in the thread, while ``In-Reply-To`` + ``References``
    headers keep native mail clients happy.
    """
    import base64
    from email.message import EmailMessage

    subject = params.get("subject", "")
    body = params.get("body", "")
    in_reply_to = params.get("in_reply_to", "")
    thread_id = params.get("thread_id", "")
    if not subject:
        log.warning("send_email_to_self: missing subject")
        return

    from deja.identity import load_user
    user = load_user()
    user_email = user.email
    if not user_email:
        log.warning("send_email_to_self: no user email on file")
        return

    # Prefix the subject so the user can filter/rule on these in Gmail.
    # Check for [Deja] anywhere in the subject — replies will have
    # "Re: [Deja] ..." and shouldn't get a second prefix.
    if "[Deja]" not in subject:
        subject = f"[Deja] {subject}"

    msg = EmailMessage()
    msg["From"] = user_email
    msg["To"] = user_email
    msg["Subject"] = subject
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg.set_content(body or "")
    encoded = base64.urlsafe_b64encode(msg.as_bytes()).decode()

    svc = _service("gmail", "v1")
    if svc is None:
        log.warning("send_email_to_self: skipped (no service)")
        return

    send_body: dict = {"raw": encoded}
    if thread_id:
        send_body["threadId"] = thread_id

    try:
        svc.users().messages().send(
            userId="me",
            body=send_body,
        ).execute()
    except Exception as e:
        log.warning("send_email_to_self failed: %s", type(e).__name__)
        return
    log.info(
        "send_email_to_self: sent '%s'%s — %s",
        subject, " (threaded)" if thread_id else "", reason,
    )
    _log_action(
        "send_email_to_self",
        f"to self: {subject}" + (" [threaded]" if thread_id else ""),
    )

    try:
        from deja.chief_of_staff import log_dialogue_turn
        log_dialogue_turn(
            role="cos",
            subject=subject,
            body=body or "",
            thread_id=thread_id,
            in_reply_to=in_reply_to,
        )
    except Exception:
        log.debug("dialogue log write failed", exc_info=True)


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

    svc = _service("tasks", "v1")
    if svc is None:
        log.warning("create_task: skipped (no service)")
        return

    try:
        svc.tasks().insert(tasklist=tasklist, body=task_body).execute()
    except Exception as e:
        log.warning("create_task failed: %s", type(e).__name__)
        return
    log.info("create_task: '%s' — %s", title, reason)
    _log_action("create_task", title)


def _complete_task(params: dict, reason: str) -> None:
    """Mark a Google Tasks task as completed."""
    task_id = params.get("task_id", "")
    tasklist = params.get("tasklist", "@default")
    if not task_id:
        log.warning("complete_task: missing task_id")
        return

    svc = _service("tasks", "v1")
    if svc is None:
        log.warning("complete_task: skipped (no service)")
        return

    try:
        svc.tasks().patch(
            tasklist=tasklist,
            task=task_id,
            body={"status": "completed"},
        ).execute()
    except Exception as e:
        log.warning("complete_task failed: %s", type(e).__name__)
        return
    log.info("complete_task: %s — %s", task_id, reason)
    _log_action("complete_task", f"completed task {task_id}")


def _notify(params: dict, reason: str) -> None:
    """Show a notification bubble from the tray icon.

    Writes to ~/.deja/notification.json which the Swift app
    polls and displays as a mini popover from the menu bar icon.
    """
    title = params.get("title", "Déjà")
    message = params.get("message", "")
    if not message:
        log.warning("notify: missing message")
        return

    import json
    from deja.config import DEJA_HOME

    notif_path = DEJA_HOME / "notification.json"
    notif_path.write_text(json.dumps({
        "title": title,
        "message": message,
        "timestamp": datetime.now().isoformat(),
    }))

    log.info("notify: '%s' — %s", message[:80], reason)
    _log_action("notify", message[:80])


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_EXECUTORS = {
    "calendar_create": _calendar_create,
    "calendar_update": _calendar_update,
    "draft_email": _draft_email,
    "send_email_to_self": _send_email_to_self,
    "create_task": _create_task,
    "complete_task": _complete_task,
    "notify": _notify,
}
