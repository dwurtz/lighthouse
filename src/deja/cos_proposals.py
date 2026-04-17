"""Pending-proposal queue for the chief-of-staff loop.

The cos loop's default is to propose action, not take it. When it
wants to do something with outbound blast radius (email a third party,
create a calendar event, add a task) it persists a proposal here and
emails the user with `[Deja Propose #<id>] <summary>` in the subject.

The user replies in natural language — "yes do it", "change the
subject to be less formal", "hold off until Monday", anything. On
the next cycle cos reads the proposal + reply together and decides
intent. Approval → execute the stored action. Tweak → re-propose
with edits. Rejection → archive.

Layout
------

    ~/.deja/cos_proposals/
      pending/<id>.json      awaiting user reply
      approved/<id>.json     executed successfully
      rejected/<id>.json     user said no, or cos withdrew
      tweaked/<id>.json      user asked for changes; cos will re-propose

Each file is a single JSON object:

    {
      "id": "a1b2c3",
      "created_ts": "2026-04-17T22:00:00Z",
      "summary": "Draft reply to Jon Sturos re: underlayment quote",
      "reason": "Jon asked about whole-house vs partial underlayment; David hasn't replied",
      "action_type": "draft_email",
      "action_params": {"to": "...", "subject": "...", "body": "..."},
      "cycle_id": "c_abc123",
      "user_reply_summary": null  // set when user responds
    }

The ID is short (6 hex) so it fits in an email subject without
eating the attention budget. Collisions are cheap to avoid — we
try a few before bailing.
"""

from __future__ import annotations

import json
import secrets
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from deja.config import DEJA_HOME

log = logging.getLogger(__name__)

PROPOSALS_ROOT = DEJA_HOME / "cos_proposals"
_STATES = ("pending", "approved", "rejected", "tweaked")


def _ensure_layout() -> None:
    for state in _STATES:
        (PROPOSALS_ROOT / state).mkdir(parents=True, exist_ok=True)


def _new_id() -> str:
    """Six-hex token. Collision detection is cheap; we retry on conflict."""
    return secrets.token_hex(3)


def _path(state: str, pid: str) -> Path:
    return PROPOSALS_ROOT / state / f"{pid}.json"


def create_proposal(
    *,
    action_type: str,
    action_params: dict,
    summary: str,
    reason: str,
    cycle_id: str = "",
) -> dict[str, Any]:
    """Persist a new pending proposal and return the record.

    Caller is responsible for emailing the user. The proposal itself
    is just a file on disk here; the notification channel is separate.
    """
    _ensure_layout()

    # Find a free id — at 6 hex tokens collisions are astronomically
    # rare for a personal queue, but be safe.
    for _ in range(5):
        pid = _new_id()
        if not any(_path(s, pid).exists() for s in _STATES):
            break
    else:
        raise RuntimeError("could not allocate a unique proposal id")

    record = {
        "id": pid,
        "created_ts": datetime.now(timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
        "summary": summary.strip(),
        "reason": reason.strip(),
        "action_type": action_type,
        "action_params": action_params or {},
        "cycle_id": cycle_id or "",
        "user_reply_summary": None,
    }
    _path("pending", pid).write_text(json.dumps(record, indent=2), encoding="utf-8")
    return record


def list_proposals(state: str = "pending") -> list[dict]:
    """Return all proposals in the given state, newest first."""
    if state not in _STATES:
        raise ValueError(f"unknown state: {state}")
    d = PROPOSALS_ROOT / state
    if not d.is_dir():
        return []
    out: list[dict] = []
    for p in sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            log.warning("skipping unparseable proposal %s", p.name)
    return out


def get_proposal(pid: str) -> tuple[str, dict] | None:
    """Locate a proposal by id across all states. Returns (state, record) or None."""
    for state in _STATES:
        p = _path(state, pid)
        if p.exists():
            try:
                return state, json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                return None
    return None


def _move(pid: str, from_state: str, to_state: str, mutations: dict | None = None) -> dict:
    """Atomically move a proposal between states, applying optional edits."""
    src = _path(from_state, pid)
    if not src.exists():
        raise FileNotFoundError(f"proposal {pid} not in {from_state}")
    record = json.loads(src.read_text(encoding="utf-8"))
    if mutations:
        record.update(mutations)
    dst = _path(to_state, pid)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(record, indent=2), encoding="utf-8")
    src.unlink()
    return record


def mark_approved(pid: str, user_reply_summary: str = "") -> dict:
    """User said yes. Caller should execute_action(record[action_type], ...)."""
    return _move(
        pid, "pending", "approved",
        mutations={
            "user_reply_summary": user_reply_summary or "approved",
            "resolved_ts": datetime.now(timezone.utc)
                .isoformat(timespec="seconds").replace("+00:00", "Z"),
        },
    )


def mark_rejected(pid: str, user_reply_summary: str = "") -> dict:
    """User said no, or cos gave up. Moves to rejected/ but preserved."""
    return _move(
        pid, "pending", "rejected",
        mutations={
            "user_reply_summary": user_reply_summary or "rejected",
            "resolved_ts": datetime.now(timezone.utc)
                .isoformat(timespec="seconds").replace("+00:00", "Z"),
        },
    )


def mark_tweaked(pid: str, user_reply_summary: str) -> dict:
    """User asked for edits. Cos is expected to create a new proposal
    (v2) with the adjustments; the original lands in tweaked/ for
    provenance."""
    return _move(
        pid, "pending", "tweaked",
        mutations={
            "user_reply_summary": user_reply_summary,
            "resolved_ts": datetime.now(timezone.utc)
                .isoformat(timespec="seconds").replace("+00:00", "Z"),
        },
    )


__all__ = [
    "PROPOSALS_ROOT",
    "create_proposal",
    "list_proposals",
    "get_proposal",
    "mark_approved",
    "mark_rejected",
    "mark_tweaked",
]
