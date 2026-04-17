"""Webhook emitter — fire configured URLs after each substantive integrate cycle.

Deja is the nervous system; routines (Claude Code Routines, or any other
webhook receiver) are the reflexes. Instead of having the agent poll
for changes on a schedule, Deja pushes a compact JSON payload to
registered URLs the moment a cycle finishes writing non-trivial
updates. The receiver decides what to do: notify the user, draft a
reply, close a loop, or stay silent.

One general-purpose webhook (rather than N specialized event types) is
the design on purpose — the decision of "what matters" is the agent's
job, not Deja's. Deja's job is to observe and to hand the observation
off the moment it's coherent.

Config
------

``~/.deja/webhooks.yaml``:

    webhooks:
      - name: chief-of-staff
        url: https://routines.claude.ai/trigger/<token>
        enabled: true

Multiple webhooks are allowed; each receives the same payload. Failures
are logged but never block the cycle. Network timeout is short (5s); we
fire-and-forget on a background thread.

Payload
-------

Intentionally small. The agent has the full MCP surface on the other
side; the webhook exists to wake it up, not to ship state.

    {
      "cycle_id": "...",
      "ts": "2026-04-17T12:45:00Z",
      "narrative": "David sent an email to Jon Sturos...",
      "wiki_update_slugs": ["people/jon-sturos", "events/2026-04-17/..."],
      "goal_changes_count": 2,
      "due_reminders_count": 1,
      "new_t1_signal_count": 3
    }

Audit
-----

Every emit records one ``audit.record()`` entry with ``action="webhook_emit"``
so ``deja hermes-trail`` can correlate webhook fires to downstream
routine actions (which come back in via MCP with ``trigger.kind=mcp``).
This closes the loop: David can see "webhook fired at 10:23, Claude
drafted email at 10:24" as consecutive audit entries.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from deja.config import DEJA_HOME

log = logging.getLogger(__name__)

WEBHOOKS_CONFIG = DEJA_HOME / "webhooks.yaml"
_HTTP_TIMEOUT_SEC = 5.0


@dataclass(frozen=True)
class Webhook:
    """One configured webhook target."""
    name: str
    url: str
    enabled: bool = True


def _load_webhooks() -> list[Webhook]:
    """Parse webhooks.yaml. Missing file or bad YAML → empty list (no-op mode)."""
    if not WEBHOOKS_CONFIG.exists():
        return []
    try:
        import yaml
        data = yaml.safe_load(WEBHOOKS_CONFIG.read_text(encoding="utf-8")) or {}
    except Exception:
        log.exception("failed to parse %s", WEBHOOKS_CONFIG)
        return []
    entries = data.get("webhooks") or []
    out: list[Webhook] = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        url = (e.get("url") or "").strip()
        if not url:
            continue
        out.append(Webhook(
            name=str(e.get("name") or "webhook"),
            url=url,
            enabled=bool(e.get("enabled", True)),
        ))
    return out


def _compact_payload(
    *,
    cycle_id: str,
    narrative: str,
    wiki_updates: Iterable[dict] | None,
    tasks_update: dict | None,
    due_reminders: list | None,
    new_t1_signal_count: int,
) -> dict[str, Any]:
    """Shape the POST body — small, agent-friendly, no duplication of state
    the receiver can pull via MCP."""
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


def _is_substantive(payload: dict) -> bool:
    """A cycle is worth a webhook fire iff *something* actionable changed.

    Pure narrative-only cycles (the agent observed but did nothing) stay
    silent so receivers aren't woken up for no-op windows. Narrative
    content still gets written to the observations file regardless.
    """
    return (
        bool(payload.get("wiki_update_slugs"))
        or int(payload.get("goal_changes_count") or 0) > 0
        or int(payload.get("due_reminders_count") or 0) > 0
        or int(payload.get("new_t1_signal_count") or 0) > 0
    )


def _post(url: str, payload: dict) -> tuple[bool, str]:
    """POST synchronously; return (ok, detail)."""
    try:
        import httpx
        r = httpx.post(url, json=payload, timeout=_HTTP_TIMEOUT_SEC)
        ok = 200 <= r.status_code < 300
        detail = f"{r.status_code}"
        if not ok:
            detail += f" {r.text[:120]}"
        return ok, detail
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:120]}"


def _fire_one(webhook: Webhook, payload: dict) -> None:
    """Fire a single webhook and record an audit entry for it."""
    ok, detail = _post(webhook.url, payload)
    try:
        from deja import audit
        reason = (payload.get("narrative") or "")[:200]
        summary = f"[{'ok' if ok else 'fail'}] {webhook.name} ({detail})"
        audit.record(
            "webhook_emit",
            target=f"webhook/{webhook.name}",
            reason=f"{summary} — {reason}",
        )
    except Exception:
        log.debug("webhook audit record failed", exc_info=True)
    if not ok:
        log.warning("webhook %s failed: %s", webhook.name, detail)


def emit_cycle_complete(
    *,
    cycle_id: str,
    narrative: str = "",
    wiki_updates: Iterable[dict] | None = None,
    tasks_update: dict | None = None,
    due_reminders: list | None = None,
    new_t1_signal_count: int = 0,
) -> None:
    """Fire registered webhooks for a completed integrate cycle.

    Non-blocking: HTTP POSTs run on a daemon thread so a slow/unreachable
    receiver can't delay the agent loop. Returns immediately.
    """
    webhooks = [w for w in _load_webhooks() if w.enabled]
    if not webhooks:
        return

    payload = _compact_payload(
        cycle_id=cycle_id,
        narrative=narrative,
        wiki_updates=wiki_updates,
        tasks_update=tasks_update,
        due_reminders=due_reminders,
        new_t1_signal_count=new_t1_signal_count,
    )
    if not _is_substantive(payload):
        log.debug("webhook: cycle had no substantive activity — skipping")
        return

    def _worker():
        for w in webhooks:
            try:
                _fire_one(w, payload)
            except Exception:
                log.exception("webhook %s worker failed", w.name)

    threading.Thread(target=_worker, daemon=True, name="deja-webhooks").start()


__all__ = [
    "WEBHOOKS_CONFIG",
    "Webhook",
    "emit_cycle_complete",
]
