"""Chief-of-staff loop — fires after each substantive integrate cycle.

This is Deja's event-driven reflex layer. After a cycle writes
something real (wiki updates, goal mutations, due reminders, T1
signals), we spawn ``claude`` non-interactively with the Deja MCP
attached. Claude reads the payload, pulls whatever state it needs
via MCP, decides whether the user needs to be pinged, and either:

  * emails the user via ``execute_action("send_email_to_self", ...)``,
    which sends immediately to their registered address (the push
    channel — readable on mobile);
  * takes a concrete action via MCP (draft a reply, close a loop,
    create a calendar event); or
  * stays silent.

The decision of "does this deserve attention" lives in the Claude
prompt, not in Deja. Deja's contribution is only firing on the right
moments and providing the context.

Config
------

``~/.deja/chief_of_staff/``:

  * ``enabled`` — empty marker file; delete to disable the loop
  * ``system_prompt.md`` — the instruction body sent to Claude on
    every invocation. A default is auto-created on first run.
  * ``mcp_config.json`` — MCP server config for the ``claude`` sub-
    process. Auto-created with just the Deja server.

Invocation
----------

  * Non-blocking (daemon thread) — never delays the agent loop
  * 10-minute subprocess timeout — runaway invocations get killed
  * Every invocation writes ``audit.record("cos_invoke", ...)``
    so ``deja hermes-trail`` shows both the trigger and what
    Claude then did via MCP (which carries ``trigger.kind=mcp``)

The loop is intentionally permission-bypassing in the spawned
``claude`` — the user pre-approves by enabling cos and trusting
the Deja MCP. Everything Claude does is audited; rollback is
always possible via git on ``~/Deja`` or
``apply_tasks_update`` undoes.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from deja.config import DEJA_HOME

log = logging.getLogger(__name__)

COS_DIR = DEJA_HOME / "chief_of_staff"
COS_ENABLED_FLAG = COS_DIR / "enabled"
COS_SYSTEM_PROMPT = COS_DIR / "system_prompt.md"
COS_MCP_CONFIG = COS_DIR / "mcp_config.json"
COS_LOG = COS_DIR / "invocations.jsonl"
_SUBPROCESS_TIMEOUT_SEC = 600  # 10 min hard cap


DEFAULT_SYSTEM_PROMPT = """\
You are the user's chief of staff, operating inside a local Claude
Code session spawned by Deja. Deja is the user's personal memory +
action layer; you reach it through the `deja` MCP server already
attached.

You were just fired because Deja completed an integrate cycle with
substantive activity. The user prompt is the payload: what happened
this cycle, in compact form.

Your job: decide what to do about it.

## Decision tree

For every invocation, pick ONE of:

1. **NOTIFY via email** — send an email to the user's own address.
   Call `execute_action("send_email_to_self", {subject, body})`.
   Subject gets auto-prefixed with "[Deja]" so the user can filter.
   The user reads these on mobile; keep subject + body terse and
   scannable. Notify when:

   - A T1 signal (user's own action or inner-circle inbound) has
     something actionable the user may miss without a nudge.
   - A waiting-for just resolved itself; worth acknowledging.
   - A reminder is due today and the answer is non-obvious.
   - Something surprising or cross-project (conflict, opportunity).

2. **ACT via MCP** — use any of the Deja MCP tools to change state
   or send action into the world:

   - `execute_action("draft_email", {to, subject, body})` — draft a
     reply in the user's voice, saved to Gmail drafts. Never send
     to third parties; always draft.
   - `execute_action("calendar_create", {...})` — create an event.
     Prefix convention: no prefix = firm, 🔔 = reminder, ❓ = open
     question / soft suggestion.
   - `complete_task`, `resolve_waiting_for`, `resolve_reminder`,
     `archive_*` — close loops aggressively when evidence supports.
   - `add_task`, `add_waiting_for`, `add_reminder` — capture gaps.
   - `update_wiki` — only if a wiki fact is stale or wrong and you
     have a concrete signal grounding the change.

3. **SILENT** — return without doing anything. The cycle's activity
   was routine context-building that doesn't need the user's
   attention or a write. If you choose this, explain why in one
   sentence in your final message so the audit trail is complete.

## How to work

- Start by calling `daily_briefing` for full state context. The
  webhook payload tells you WHAT changed THIS cycle; the briefing
  tells you WHERE EVERYTHING STANDS. You need both.
- Before drafting an email to a person, call `get_page("people", slug)`
  to ground it in their context.
- Every MCP mutation writes an audit entry tagged
  `trigger.kind=mcp, trigger.detail=hermes` — the user reviews with
  `deja hermes-trail`. Make your `reason` field concrete and cite
  the triggering signal.
- Never fabricate. If the wiki doesn't say it, don't invent it.
- Close loops aggressively. Stale items are failure modes. Indirect
  satisfaction counts (a forwarded contact, a delegated reach-out,
  the promised info arriving via the promised person).

## Tone — when you notify

The user is a builder. Terse. Specific. Actionable. One line for
the what, one for the proposed next action if any. Never pad.

Good subject: "Jon replied — tile roof needs re-lay, quote in ~1wk"
Good body:
> Jon Sturos replied (07:53): flashing looks fine; affected deck
> area needs new underlayment + re-lay. Quote coming next week.
> Drafted an ack-and-confirm reply waiting in your Gmail drafts.

Bad: "Hi David! Jon sent you a thoughtful reply about the roof,
and I thought you might want to know. Would you like me to help
you respond?"

## Payload shape (user message)

    {
      "cycle_id": "...",
      "ts": "2026-04-17T...Z",
      "narrative": "one-paragraph prose summary of what the
        integrate cycle just observed and wrote",
      "wiki_update_slugs": ["category/slug", ...],
      "goal_changes_count": N,
      "due_reminders_count": N,
      "new_t1_signal_count": N
    }

Do the work now. End your response with a single sentence describing
what you did (or why you stayed silent) — that becomes the final
audit line.
"""


def _ensure_cos_dir() -> None:
    """First-run setup: create config directory and default files.

    The existence of the ``enabled`` flag file is what turns the loop
    on; we create it in here so ``deja cos enable`` is a simple
    ``touch`` and ``disable`` is an ``rm``.
    """
    COS_DIR.mkdir(parents=True, exist_ok=True)

    if not COS_SYSTEM_PROMPT.exists():
        COS_SYSTEM_PROMPT.write_text(DEFAULT_SYSTEM_PROMPT, encoding="utf-8")

    if not COS_MCP_CONFIG.exists():
        # Default: Deja MCP from the installed app bundle (Claude
        # Desktop convention). If the user prefers the dev venv they
        # can hand-edit this file.
        bundled_python = (
            "/Applications/Deja.app/Contents/Resources/python-env/bin/python3"
        )
        command = bundled_python if Path(bundled_python).exists() else "python3"
        config = {
            "mcpServers": {
                "deja": {
                    "command": command,
                    "args": ["-m", "deja", "mcp"],
                }
            }
        }
        COS_MCP_CONFIG.write_text(
            json.dumps(config, indent=2), encoding="utf-8"
        )


def is_enabled() -> bool:
    return COS_ENABLED_FLAG.exists()


def enable() -> None:
    _ensure_cos_dir()
    COS_ENABLED_FLAG.touch()


def disable() -> None:
    if COS_ENABLED_FLAG.exists():
        COS_ENABLED_FLAG.unlink()


def _claude_binary() -> str | None:
    """Return the path to the ``claude`` CLI, or None if unavailable."""
    return shutil.which("claude")


def _build_payload(
    *,
    cycle_id: str,
    narrative: str,
    wiki_updates: Iterable[dict] | None,
    tasks_update: dict | None,
    due_reminders: list | None,
    new_t1_signal_count: int,
) -> dict[str, Any]:
    slugs: list[str] = []
    for u in wiki_updates or []:
        cat = u.get("category") or ""
        slug = u.get("slug") or ""
        if cat and slug:
            slugs.append(f"{cat}/{slug}")

    goal_changes = 0
    for key in (
        "add_tasks", "complete_tasks", "archive_tasks",
        "add_waiting", "resolve_waiting", "archive_waiting",
        "add_reminders", "resolve_reminders", "archive_reminders",
    ):
        goal_changes += len((tasks_update or {}).get(key) or [])

    return {
        "cycle_id": cycle_id or "",
        "ts": datetime.now(timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
        "narrative": (narrative or "").strip(),
        "wiki_update_slugs": slugs[:20],
        "goal_changes_count": goal_changes,
        "due_reminders_count": len(due_reminders or []),
        "new_t1_signal_count": int(new_t1_signal_count or 0),
    }


def _run_claude(payload: dict) -> tuple[int, str, str]:
    """Spawn ``claude -p`` with the payload as the user message."""
    claude_bin = _claude_binary()
    if not claude_bin:
        return (127, "", "claude CLI not found on PATH")

    cmd = [
        claude_bin,
        "-p", json.dumps(payload),
        "--append-system-prompt-file", str(COS_SYSTEM_PROMPT),
        "--mcp-config", str(COS_MCP_CONFIG),
        "--dangerously-skip-permissions",
        "--output-format", "text",
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT_SEC,
            env={**os.environ},
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return (124, "", f"subprocess exceeded {_SUBPROCESS_TIMEOUT_SEC}s")
    except Exception as e:
        return (1, "", f"{type(e).__name__}: {e}")


def _log_invocation(
    *,
    cycle_id: str,
    payload: dict,
    rc: int,
    stdout: str,
    stderr: str,
) -> None:
    """Persist one line per invocation to ~/.deja/chief_of_staff/invocations.jsonl.

    Complements the audit log: the full claude output is captured
    here so the user can inspect why the agent chose what it chose.
    """
    try:
        COS_LOG.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": datetime.now(timezone.utc)
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z"),
            "cycle_id": cycle_id,
            "payload": payload,
            "rc": rc,
            "stdout": stdout[-4000:],
            "stderr": stderr[-2000:],
        }
        with COS_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        log.debug("cos invocation log write failed", exc_info=True)

    try:
        from deja import audit
        summary = "ok" if rc == 0 else f"rc={rc}"
        final_line = (stdout or "").strip().splitlines()[-1:]
        final = final_line[0] if final_line else ""
        audit.record(
            "cos_invoke",
            target=f"cycle/{cycle_id}",
            reason=f"{summary} — {final[:200]}",
        )
    except Exception:
        log.debug("cos audit record failed", exc_info=True)


def invoke(
    *,
    cycle_id: str,
    narrative: str = "",
    wiki_updates: Iterable[dict] | None = None,
    tasks_update: dict | None = None,
    due_reminders: list | None = None,
    new_t1_signal_count: int = 0,
) -> None:
    """Fire the chief-of-staff loop if enabled. Non-blocking."""
    if not is_enabled():
        return
    if not COS_SYSTEM_PROMPT.exists() or not COS_MCP_CONFIG.exists():
        _ensure_cos_dir()

    payload = _build_payload(
        cycle_id=cycle_id,
        narrative=narrative,
        wiki_updates=wiki_updates,
        tasks_update=tasks_update,
        due_reminders=due_reminders,
        new_t1_signal_count=new_t1_signal_count,
    )

    def _worker():
        try:
            rc, stdout, stderr = _run_claude(payload)
            _log_invocation(
                cycle_id=cycle_id,
                payload=payload,
                rc=rc,
                stdout=stdout,
                stderr=stderr,
            )
        except Exception:
            log.exception("cos worker failed")

    threading.Thread(target=_worker, daemon=True, name="deja-cos").start()


__all__ = [
    "COS_DIR",
    "COS_ENABLED_FLAG",
    "COS_SYSTEM_PROMPT",
    "COS_MCP_CONFIG",
    "COS_LOG",
    "DEFAULT_SYSTEM_PROMPT",
    "is_enabled",
    "enable",
    "disable",
    "invoke",
]
