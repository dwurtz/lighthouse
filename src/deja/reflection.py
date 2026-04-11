"""Back-compat shim for the old reflection pass.

The periodic "deep wiki pass" is now implemented as vector-based dedup
(see ``deja.dedup``). This module exists only to preserve the legacy
import surface used by ``agent/loop.py``, the reflection-schedule tests,
and the ``tools/reflection_eval.py`` evaluation harness.

Everything the callers actually exercise lives in
``deja.reflection_scheduler`` or ``deja.dedup``. The signal/event text
builders below are retained because the eval harness still walks them
to snapshot fixtures against historical prompts.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from deja.config import OBSERVATIONS_LOG, WIKI_DIR

# Re-export the scheduler public API. Callers that do
# ``from deja.reflection import should_run_reflection, run_reflection``
# continue to work unchanged, and ``tests/test_reflect_schedule.py``
# monkeypatches ``deja.reflection.REFLECT_SLOT_HOURS`` which routes
# through the re-export.
from deja.reflection_scheduler import (  # noqa: F401
    should_run_reflection,
    run_reflection,
    _most_recent_slot,
    _read_last_run,
    _write_last_run,
    REFLECT_SLOT_HOURS,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Signal / event text builders (used by tools/reflection_eval.py fixtures)
# ---------------------------------------------------------------------------


def _recent_signals_text(days: int = 7, max_chars: int = 500_000) -> str:
    """Build the recent-observations block for legacy reflect fixtures."""
    path = OBSERVATIONS_LOG
    if not path.exists():
        return "(no signals)"
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    lines_out: list[str] = []
    for line in path.read_text().splitlines():
        try:
            s = json.loads(line)
            ts = s.get("timestamp", "")
            try:
                t = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
            except Exception:
                continue
            if t < cutoff:
                continue
            source = s.get("source", "?")
            sender = s.get("sender", "")
            text = (s.get("text", "") or "")[:2000]
            lines_out.append(f"[{ts[:19]}] [{source}] {sender}: {text}")
        except Exception:
            continue
    out = "\n".join(lines_out[-10_000:])
    if len(out) > max_chars:
        out = out[-max_chars:]
    return out or "(no recent signals)"


def _recent_events_text(days: int = 7) -> str:
    """Read all event pages from the last N days for legacy reflect fixtures."""
    events_dir = WIKI_DIR / "events"
    if not events_dir.is_dir():
        return "(no events yet)"

    from datetime import date as _date
    today = _date.today()
    cutoff = today - timedelta(days=days)
    entries: list[tuple[str, str]] = []

    for day_dir in sorted(events_dir.iterdir()):
        if not day_dir.is_dir():
            continue
        try:
            dir_date = _date.fromisoformat(day_dir.name)
        except ValueError:
            continue
        if dir_date < cutoff:
            continue
        for event_file in sorted(day_dir.glob("*.md")):
            try:
                content = event_file.read_text(encoding="utf-8", errors="replace")
                entries.append((f"{day_dir.name}/{event_file.stem}", content.strip()))
            except OSError:
                continue

    if not entries:
        return "(no events in the last 7 days)"

    lines = []
    for slug, content in entries:
        lines.append(f"### events/{slug}\n\n{content}")
    return "\n\n---\n\n".join(lines)
