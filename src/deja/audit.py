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
      "target": "people/jane-doe" | "goals/tasks" | ...,
      "reason": "<verbatim LLM reason or deterministic justification>"
    }

Grep examples:

    jq 'select(.target == "people/jane-doe")' ~/.deja/audit.jsonl
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
    "signal_ids": [],
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
    _context["signal_ids"] = []


def set_signals(signal_ids: list[str]) -> None:
    """Register the signal id_keys that seeded the current cycle.

    Called after the cycle's signal batch is finalized (post-triage). Every
    subsequent ``record()`` in this cycle gets a ``signal_ids`` field so we
    can trace any wiki write / event / goal-action back to the raw
    observations that caused it. Capped at 200 ids per entry — more than
    that in one cycle would be unusual and the audit line shouldn't grow
    without bound.
    """
    _context["signal_ids"] = [s for s in (signal_ids or []) if s][:200]


def clear_context() -> None:
    _context["cycle"] = ""
    _context["trigger"] = {"kind": "manual", "detail": ""}
    _context["signal_ids"] = []


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
        # Thread the seeding signal ids onto every cycle-scoped audit
        # entry so annotation tools can join writes to their actual
        # inputs instead of guessing via time windows.
        sids = _context.get("signal_ids") or []
        if sids:
            entry["signal_ids"] = list(sids)
        # Stamp the active request id onto every new audit entry so
        # cycle- and request-correlated traces are possible. Existing
        # rows without this field remain valid — it's purely additive.
        try:
            from deja.observability import current_request_id

            rid = current_request_id()
            if rid:
                entry["request_id"] = rid
        except Exception:
            pass
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(AUDIT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception:
        log.exception("audit.record failed (action=%s target=%s)", action, target)


def trim_older_than(days: int = 7) -> int:
    """Drop audit entries whose ``ts`` is older than ``days`` ago.

    Keeps the file bounded — the oldest entries are purged, so the
    working window is roughly the last ``days`` days. Returns the
    number of rows dropped. Run on process startup and once every
    reflect cycle; cheap even at tens of thousands of rows.

    Atomicity: writes to a tmp sibling and ``os.replace``-s it in,
    so a crash mid-trim leaves the original intact. A concurrent
    ``record()`` call during trim may have its append land on the
    old file that's about to be replaced — the worst case is losing
    one freshly-written row, so we accept it rather than take a
    cross-process lock.
    """
    import os
    import tempfile
    from datetime import datetime, timezone, timedelta

    if not AUDIT_LOG.exists():
        return 0

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    kept: list[str] = []
    dropped = 0
    try:
        with open(AUDIT_LOG, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    rec = json.loads(stripped)
                    ts_str = (rec.get("ts") or "").replace("Z", "+00:00")
                    ts = datetime.fromisoformat(ts_str)
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                except Exception:
                    # Unparseable row — keep. Trim is for age, not repair.
                    kept.append(line)
                    continue
                if ts >= cutoff:
                    kept.append(line)
                else:
                    dropped += 1
    except OSError:
        log.exception("audit.trim: failed to read %s", AUDIT_LOG)
        return 0

    if dropped == 0:
        return 0

    try:
        fd, tmp_path = tempfile.mkstemp(
            prefix=".audit.", suffix=".tmp", dir=str(AUDIT_LOG.parent)
        )
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.writelines(kept)
        os.replace(tmp_path, AUDIT_LOG)
        log.info("audit.trim: dropped %d rows older than %dd (%d kept)",
                 dropped, days, len(kept))
    except Exception:
        log.exception("audit.trim: atomic replace failed")
        return 0

    return dropped


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
