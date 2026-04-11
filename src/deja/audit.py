"""Single source of truth for agent action audits.

Every discrete mutation the agent makes — wiki write/delete, task add/
complete/archive, waiting-for add/resolve/archive, reminder add/resolve/
archive, goal_action execution, dedup merge, user command, voice
transcript — appends one line to ``~/.deja/audit.jsonl``.

Schema (one JSON object per line):

    {
      "ts": "2026-04-25T08:14:02Z",
      "cycle": "c_abc123def456",
      "trigger": {
        "kind": "signal" | "reminder" | "user_cmd" | "expiry"
              | "dedup" | "onboarding" | "startup" | "manual",
        "detail": "..."
      },
      "action": "wiki_write" | "wiki_delete" | "event_create"
              | "task_add" | "task_complete" | "task_archive"
              | "waiting_add" | "waiting_resolve" | "waiting_archive"
              | "reminder_add" | "reminder_resolve" | "reminder_archive"
              | "goal_action" | "dedup_merge" | "user_command"
              | "voice_transcript" | "automation_add" | "onboarding_step"
              | "health_check",
      "target": "people/amanda-peffer" | "goals/tasks" | ...,
      "reason": "<verbatim LLM reason or deterministic justification>"
    }

Grep examples:

    jq 'select(.target == "people/amanda-peffer")' ~/.deja/audit.jsonl
    jq 'select(.trigger.kind == "reminder")' ~/.deja/audit.jsonl
    jq 'select(.cycle == "c_abc123")' ~/.deja/audit.jsonl

This module replaces the former triplet of ``activity_log.append_log_entry``
(human-readable ``~/Deja/log.md``), ``agent/integration.log_analysis``
(legacy ``~/.deja/integrations.jsonl``), and scattered ``log.info`` audit
lines in Python's stdlib logger. ``~/.deja/deja.log`` is still the place
for runtime exceptions and startup diagnostics — audit.jsonl is the
answer to "why did this happen".
"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timezone
from typing import Optional

from deja.config import DEJA_HOME

log = logging.getLogger(__name__)

AUDIT_LOG = DEJA_HOME / "audit.jsonl"


_context: dict = {
    "cycle": "",
    "trigger": {"kind": "manual", "detail": ""},
}


def new_cycle_id() -> str:
    """Return a short, collision-resistant cycle identifier."""
    return "c_" + secrets.token_hex(6)


def set_context(cycle: str, trigger_kind: str, trigger_detail: str = "") -> None:
    """Register the current cycle + trigger for subsequent record() calls.

    Called once at the top of ``run_analysis_cycle`` (and other top-level
    agent entry points). Every ``record()`` written until the next
    ``set_context`` / ``clear_context`` carries these fields implicitly
    so individual call sites don't have to thread them through every
    function signature.
    """
    _context["cycle"] = cycle or ""
    _context["trigger"] = {
        "kind": trigger_kind or "manual",
        "detail": trigger_detail or "",
    }


def clear_context() -> None:
    _context["cycle"] = ""
    _context["trigger"] = {"kind": "manual", "detail": ""}


def record(
    action: str,
    target: str,
    reason: str,
    *,
    cycle: Optional[str] = None,
    trigger: Optional[dict] = None,
) -> None:
    """Append one structured audit entry. The only writer for audit.jsonl.

    ``action``, ``target``, and ``reason`` are always written. ``cycle``
    and ``trigger`` default to whatever the current context holds;
    callers outside an agent cycle (manual CLI tools, setup, health
    checks) may pass them explicitly.
    """
    try:
        entry = {
            "ts": datetime.now(timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
            "cycle": cycle if cycle is not None else _context["cycle"],
            "trigger": trigger if trigger is not None else dict(_context["trigger"]),
            "action": action,
            "target": target,
            "reason": reason or "",
        }
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(AUDIT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception:
        log.exception("audit.record failed (action=%s target=%s)", action, target)


def read_recent(limit: int = 50, kind: Optional[str] = None) -> list[dict]:
    """Return the most recent audit entries, newest first.

    ``kind`` filters by ``trigger.kind`` if provided (e.g. 'reminder').
    Used by the ``/api/activity`` endpoint and ad-hoc debugging.
    """
    if not AUDIT_LOG.exists():
        return []
    entries: list[dict] = []
    try:
        for line in AUDIT_LOG.read_text(encoding="utf-8").splitlines()[-2000:]:
            try:
                d = json.loads(line)
            except Exception:
                continue
            if kind and (d.get("trigger") or {}).get("kind") != kind:
                continue
            entries.append(d)
    except Exception:
        return []
    entries.reverse()
    return entries[:limit] if limit and limit > 0 else entries
