"""Dedup scheduling — when to run, cooldowns, last-run marker.

Formerly the reflection scheduler; the LLM-only reflect/deduplicate pass
has been replaced by the vector+Flash-Lite dedup pipeline in
``deja.dedup``. The function names and marker file are kept as
``*_reflection`` because callers and tests still import them under those
names, and the cadence (3x/day slot boundaries) is unchanged. Think of
"reflection" here as "the periodic deep wiki pass that runs a few times
a day" — today that pass is dedup.
"""

from __future__ import annotations

import asyncio
import logging
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
    yesterday's last slot (the clock hasn't crossed today's earliest slot
    yet, so the "current" slot is still yesterday's final one).
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
    """Return the timestamp of the last successful reflection run, or None."""
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
    """Record `ts` (or now) as the last successful reflection run."""
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

    Clock-aligned, not interval-based. With default slots (02:00, 11:00,
    18:00), "did reflection run since the last time the clock crossed any
    slot boundary?" means:

      - It's 12:00 today, last run was today at 03:00 -> run (last run
        predates today's 11:00 slot that just passed)
      - It's 12:00 today, last run was today at 11:30 -> don't run
        (last run is past today's most recent slot)
      - It's 01:30 today, last run was yesterday at 19:00 -> don't run
        (last run is past yesterday's final slot of 18:00; today's 02:00
        hasn't happened yet)
      - Machine was asleep all day; wakes at 20:00 with last run 6 days
        ago -> run ONCE (backs up to today's 18:00 slot, not a stampede)

    All times are local.
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


async def run_reflection() -> dict:
    """Run the 3x/day reflect phase — a pipeline of cluster-based
    sweeps over the wiki corpus. Returns a merged summary dict.

    "Reflect" is no longer one big LLM call (the original expensive
    pass was retired). It's now a series of sweeps that reuse the
    same QMD vector embeddings at different similarity thresholds
    and filters, each producing a different kind of action:

    1. **dedup** — similarity ≥0.82 on people/projects → merge pages
       that describe the same entity. Writes directly to the wiki.
    2. **contradictions** — similarity 0.65-0.82 on people/projects →
       flag pages that reference the same subject but disagree on a
       fact. Writes directly to the wiki.
    3. **event themes** — similarity ≥0.55 on events with ``projects:
       []`` and shared people → propose a new project page. Writes
       a ``create_project`` observation; integrate creates the page.

    All three run in sequence, share one concurrency lock, and share
    one last-run marker. Per-sweep infra failures raise up to the
    agent loop so the marker isn't updated and the next heartbeat
    retries.

    Concurrent invocations are coalesced: the second caller sees the
    lock held and returns ``{"skipped": "concurrent"}`` immediately
    rather than double-running the full pipeline.
    """
    if _run_lock.locked():
        log.info("Reflect already running — skipping concurrent invocation")
        return {"skipped": "concurrent"}

    async with _run_lock:
        from deja.dedup import run_dedup
        from deja.events_to_projects import run_events_to_projects

        # 1. Dedup — merges same-entity pages.
        result = await run_dedup()

        # 2. Contradictions sweep DISABLED 2026-04-14.
        # Both Flash-Lite and Flash produced too many false positives:
        # complementary mentions of the same entity were being labeled as
        # contradictions and real facts were being stripped from pages
        # (Lei Yang interview, Archie Abrams Shopify role, Jonny irrigation
        # confirmation, etc. — all true claims removed). Net signal-to-
        # noise was negative across two days of audit data. Re-enable
        # only after a redesign that gates more strictly (date-stamped
        # claims required, higher similarity threshold, or a different
        # detection approach altogether).

        # 3. Events → projects — materializes project pages for
        #    dangling slugs + recurring event themes.
        etp_result = await run_events_to_projects()
        if isinstance(result, dict) and isinstance(etp_result, dict):
            result["events_to_projects"] = etp_result

        # 4. Audit trim — keep audit.jsonl bounded to ~7 days.
        try:
            from deja.audit import trim_older_than
            dropped = trim_older_than(days=7)
            if isinstance(result, dict):
                result["audit_trim"] = {"dropped": dropped}
        except Exception:
            log.exception("reflect: audit trim failed")

        _write_last_run()
        return result
