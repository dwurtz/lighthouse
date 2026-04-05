"""Shared step-runner for all onboarding backfill jobs.

An onboarding step (sent-email backfill, iMessage backfill, WhatsApp
backfill, future steps like calendar history, drive exports, etc.) is
just:

  1. A ``name`` string used as the marker key and log label.
  2. A ``fetch_fn`` that returns a list of ``Observation`` objects
     representing the historical context to ingest.

Everything else — identity check, marker idempotency, batching,
sequential LLM calls, wiki-lock acquisition, per-batch activity log
entries, progress callbacks, final marker write — lives here so it
only has to be right once.

Usage::

    from lighthouse.onboarding.runner import run_step
    from lighthouse.observations.email import fetch_sent_threads_backfill

    summary = await run_step(
        name="sent_email_backfill",
        fetch_fn=lambda: fetch_sent_threads_backfill(days=30),
        wiki_lock=loop._wiki_lock,
        gemini=loop.gemini,
    )
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from lighthouse.observations.types import Observation
from lighthouse import wiki as wiki_store

log = logging.getLogger(__name__)


# One LLM call per this many observations. 20 works for email threads
# (~2KB each) and for per-contact message digests (~3KB each) while
# staying well under context limits and letting the user watch pages
# appear batch-by-batch in the notch.
BATCH_SIZE = 20


# ---------------------------------------------------------------------------
# Formatting — matches AgentLoop._format_signals shape
# ---------------------------------------------------------------------------


def format_batch(observations: list[Observation]) -> str:
    """Render observations the same way the steady-state analysis cycle does.

    Keeps the onboarding prompt's ``signals_text`` block isomorphic with
    what integrate.md sees, so the LLM doesn't have to learn two formats.
    """
    lines: list[str] = []
    for obs in observations:
        ts = obs.timestamp.strftime("%Y-%m-%d %H:%M")
        lines.append(f"[{ts}] [{obs.source}] {obs.sender}: {obs.text}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Step runner
# ---------------------------------------------------------------------------


ProgressCb = Callable[[dict[str, Any]], None]
FetchFn = Callable[[], list[Observation]]


async def run_step(
    *,
    name: str,
    fetch_fn: FetchFn,
    wiki_lock: asyncio.Lock | None = None,
    gemini: Any = None,
    force: bool = False,
    on_progress: ProgressCb | None = None,
    pre_check: Callable[[], dict[str, Any] | None] | None = None,
) -> dict[str, Any]:
    """Run one onboarding step end-to-end.

    ``name`` is the step identifier used for both the marker file key
    and the activity-log prefix. Current steps: ``sent_email_backfill``,
    ``imessage_backfill``, ``whatsapp_backfill``.

    ``fetch_fn`` is called once in a thread executor (it may block on
    subprocess/SQLite I/O) and must return the complete list of
    ``Observation``s to ingest for this step. Return an empty list to
    no-op the step — the marker will still be written so it doesn't
    retry next startup.

    ``pre_check`` is an optional synchronous callable that runs before
    the fetch. If it returns a dict, that dict is treated as a "skip
    reason" and returned immediately without fetching or marking the
    step complete — useful for "db not accessible, try again after
    the user grants Full Disk Access" situations.

    Returns a summary dict suitable for logging. On already-done:
    ``{"skipped": "already_done"}``. On pre-check skip:
    the pre_check's returned dict. On empty fetch:
    ``{"threads_fetched": 0, ...}`` with the marker still written.
    """
    from lighthouse.onboarding.backfill import (
        is_step_done,
        mark_step_done,
    )
    from lighthouse.identity import load_user

    # 1. Idempotency — bail if already done (unless forced)
    if not force and is_step_done(name):
        log.info("Onboarding step %s already complete, skipping", name)
        return {"skipped": "already_done", "step": name}

    # 2. Identity sanity check — onboarding prompts rely on the self-page
    user = load_user()
    if user.is_generic or not user.email:
        log.warning(
            "Onboarding step %s aborted: no self-page with email configured",
            name,
        )
        return {"skipped": "no_self_page", "step": name}

    # 3. Per-step pre-check (e.g. Full Disk Access for iMessage/WhatsApp)
    if pre_check is not None:
        try:
            pre_result = pre_check()
        except Exception:
            log.exception("pre_check for %s raised; treating as skip", name)
            pre_result = {"skipped": "pre_check_error"}
        if pre_result is not None:
            log.info("Onboarding step %s pre-check skip: %s", name, pre_result)
            _append_log(name, f"skipped — {pre_result.get('skipped', 'unknown')}")
            return {**pre_result, "step": name}

    # 4. Fetch observations (may block — run in executor)
    loop = asyncio.get_running_loop()
    log.info("Onboarding step %s: fetching observations", name)
    try:
        observations: list[Observation] = await loop.run_in_executor(None, fetch_fn)
    except Exception:
        log.exception("Onboarding step %s fetch failed", name)
        return {"skipped": "fetch_failed", "step": name}

    log.info("Onboarding step %s: fetched %d observations", name, len(observations))

    if not observations:
        mark_step_done(name, {"observations_fetched": 0, "reason": "empty_fetch"})
        _append_log(name, "nothing to ingest — marking step complete")
        return {
            "step": name,
            "observations_fetched": 0,
            "batches_run": 0,
            "pages_written": 0,
        }

    # 5. LLM client + lock defaults for standalone CLI use
    if gemini is None:
        from lighthouse.llm_client import GeminiClient
        gemini = GeminiClient()
    if wiki_lock is None:
        wiki_lock = asyncio.Lock()

    total_batches = (len(observations) + BATCH_SIZE - 1) // BATCH_SIZE
    batches_run = 0
    pages_written = 0

    _append_log(
        name,
        f"starting — {len(observations)} observations across "
        f"{total_batches} batch(es)",
    )

    # 6. Sequential batches. Keep it sequential so the LLM sees
    #    previously-written pages when building later batches, and so
    #    the user watches pages appear in a predictable order.
    for start in range(0, len(observations), BATCH_SIZE):
        batch = observations[start:start + BATCH_SIZE]
        signals_text = format_batch(batch)

        # Re-read the wiki each batch so consolidation works across
        # batches (second batch sees the pages first batch just wrote).
        wiki_text = wiki_store.render_for_prompt()

        log.info(
            "Onboarding %s: batch %d/%d (%d obs, wiki=%d pages)",
            name, batches_run + 1, total_batches, len(batch),
            len(wiki_store.read_all_pages()),
        )

        try:
            result = await gemini.onboard_from_observations(
                signals_text=signals_text,
                wiki_text=wiki_text,
            )
        except Exception:
            log.exception(
                "Onboarding %s batch %d failed — continuing",
                name, batches_run + 1,
            )
            batches_run += 1
            continue

        updates = result.get("wiki_updates", []) or []
        reasoning = result.get("reasoning", "")
        log.info(
            "Onboarding %s batch %d → %d updates (%s)",
            name, batches_run + 1, len(updates), reasoning[:120],
        )

        if updates:
            async with wiki_lock:
                applied = await loop.run_in_executor(
                    None, lambda u=updates: wiki_store.apply_updates(u)
                )
            pages_written += applied

        batches_run += 1

        _append_log(
            name,
            f"batch {batches_run}/{total_batches} — "
            f"{len(updates)} page update(s)",
        )

        if on_progress is not None:
            try:
                on_progress({
                    "step": name,
                    "batch": batches_run,
                    "total_batches": total_batches,
                    "pages_written": pages_written,
                })
            except Exception:
                log.debug("on_progress callback failed", exc_info=True)

    summary = {
        "step": name,
        "observations_fetched": len(observations),
        "batches_run": batches_run,
        "pages_written": pages_written,
    }
    mark_step_done(name, summary)
    _append_log(
        name,
        f"complete — {pages_written} page update(s) from "
        f"{len(observations)} observation(s)",
    )
    log.info("Onboarding step %s complete: %s", name, summary)
    return summary


def _append_log(step_name: str, message: str) -> None:
    """Best-effort activity-log entry so the user sees progress in log.md."""
    try:
        from lighthouse.activity_log import append_log_entry
        append_log_entry("onboard", f"[{step_name}] {message}")
    except Exception:
        log.debug("onboarding activity_log append failed", exc_info=True)
