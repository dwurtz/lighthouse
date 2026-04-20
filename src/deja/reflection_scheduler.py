"""Reflect scheduling — when to run, cooldowns, last-run marker.

The reflect pass fires 3x/day at the slot boundaries configured in
``REFLECT_SLOT_HOURS``. Its job is to wake cos (the chief-of-staff
Claude Code subprocess) and let cos decide what deep-wiki work — if
any — needs doing. Cos reaches the four candidate-generator MCP
tools (``find_dedup_candidates``, ``find_orphan_event_clusters``,
``find_open_loops_with_evidence``, ``find_contradictions``) and acts
on whatever it finds via the existing write tools.

Before invoking cos we refresh the QMD vector index so every
candidate sweep cos runs sees the current wiki state.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
from datetime import datetime, timedelta, timezone

from deja.config import (
    REFLECT_SLOT_HOURS,
    DEJA_HOME,
)

log = logging.getLogger(__name__)

# Persistent marker of the last successful reflection run. The agent
# loop checks this on startup and at the start of every integration
# cycle — if the last run predates the most recent slot boundary,
# reflection is triggered inline.
_LAST_RUN_FILE = DEJA_HOME / "last_reflection_run"
_LEGACY_LAST_RUN = DEJA_HOME / "last_nightly_run"
if _LEGACY_LAST_RUN.exists() and not _LAST_RUN_FILE.exists():
    try:
        _LEGACY_LAST_RUN.rename(_LAST_RUN_FILE)
    except OSError:
        pass

# Lock that serializes reflection runs.
_run_lock = asyncio.Lock()


# ---------------------------------------------------------------------------
# Slot calculation
# ---------------------------------------------------------------------------

def _most_recent_slot(now: datetime) -> datetime:
    """Return the most recent reflect slot boundary at or before ``now``.

    Walks the configured ``REFLECT_SLOT_HOURS`` in local time. If any of
    today's slots is <= now, returns the latest one. Otherwise returns
    yesterday's last slot.
    """
    if not REFLECT_SLOT_HOURS:
        return now
    today_slots = [
        now.replace(hour=h, minute=0, second=0, microsecond=0)
        for h in REFLECT_SLOT_HOURS
    ]
    past = [s for s in today_slots if s <= now]
    if past:
        return past[-1]
    return today_slots[-1] - timedelta(days=1)


# ---------------------------------------------------------------------------
# Last-run marker
# ---------------------------------------------------------------------------

def _read_last_run() -> datetime | None:
    try:
        raw = _LAST_RUN_FILE.read_text().strip()
    except (OSError, FileNotFoundError):
        return None
    try:
        ts = datetime.fromisoformat(raw)
    except ValueError:
        log.warning("last_reflection_run file has unparseable content: %r", raw)
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def _write_last_run(ts: datetime | None = None) -> None:
    if ts is None:
        ts = datetime.now(timezone.utc)
    try:
        DEJA_HOME.mkdir(parents=True, exist_ok=True)
        _LAST_RUN_FILE.write_text(ts.isoformat())
    except OSError:
        log.exception("Failed to write last_reflection_run marker")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def should_run_reflection(now: datetime | None = None) -> bool:
    """Return True if reflection hasn't run since the most recent slot boundary.

    Clock-aligned, not interval-based. All times are local.
    """
    if now is None:
        now = datetime.now().astimezone()
    elif now.tzinfo is None:
        now = now.astimezone()

    last = _read_last_run()
    if last is None:
        return True

    threshold = _most_recent_slot(now)
    return last.astimezone(threshold.tzinfo) < threshold


def _refresh_qmd_index() -> None:
    """Refresh QMD so every candidate sweep sees the current wiki state.

    Failure here is non-fatal — we still let cos run against the last
    known embeddings rather than skipping the reflect pass — but we
    log loudly so stale-index drift is visible.
    """
    try:
        subprocess.run(["qmd", "update"], capture_output=True, timeout=60, check=False)
        subprocess.run(["qmd", "embed"], capture_output=True, timeout=300, check=False)
    except Exception:
        log.exception("reflect: qmd refresh failed — proceeding with stale index")


async def run_reflection() -> dict:
    """Fire the reflect pass: refresh embeddings, then let cos decide.

    Steps:
      1. Refresh the QMD vector index so the candidate-generator MCP
         tools cos calls see the current wiki state.
      2. Invoke cos in reflective mode. Cos owns the loop — it uses
         ``find_dedup_candidates``, ``find_orphan_event_clusters``,
         ``find_open_loops_with_evidence``, and ``find_contradictions``
         to find work, verifies via ``get_page`` / ``gmail_search`` /
         ``search_deja`` / ``recent_activity`` as needed, and writes
         directly through the existing MCP write tools.
      3. Audit-trim to keep ``audit.jsonl`` bounded.

    Concurrent invocations are coalesced: the second caller sees the
    lock held and returns ``{"skipped": "concurrent"}`` immediately.
    """
    if _run_lock.locked():
        log.info("Reflect already running — skipping concurrent invocation")
        return {"skipped": "concurrent"}

    async with _run_lock:
        from deja import chief_of_staff

        _refresh_qmd_index()

        result: dict = {}

        if chief_of_staff.is_enabled():
            rc, stdout, stderr = await asyncio.get_running_loop().run_in_executor(
                None, chief_of_staff.invoke_reflective_sync,
            )
            final_line = (stdout or "").strip().splitlines()[-1:]
            result["cos_reflective"] = {
                "rc": rc,
                "summary": (final_line[0][:200] if final_line else ""),
            }
        else:
            result["cos_reflective"] = {"rc": 0, "summary": "(cos disabled)"}

        try:
            from deja.audit import trim_older_than
            dropped = trim_older_than(days=7)
            result["audit_trim"] = {"dropped": dropped}
        except Exception:
            log.exception("reflect: audit trim failed")

        _write_last_run()
        return result
