"""Analysis (integrate) cycle — extracted from AgentLoop.

Reads unanalyzed signals, triages them, runs Flash integration,
applies wiki updates, and logs results.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

from deja import audit
from deja import wiki as wiki_store
from deja.config import DEJA_HOME
from deja.observability import (
    DejaError,
    LLMError,
    report_error,
    request_scope,
)

# Track consecutive integrate failures so we can escalate from silent
# "internal warning" to a user-visible toast once things have been
# failing for a while. Reset to 0 on the first success.
_consecutive_integrate_failures: int = 0
_CONSECUTIVE_FAIL_THRESHOLD = 3

log = logging.getLogger(__name__)

# Defense in depth for the Swift-side screenshot defer fix: drop any
# screenshot signal older than this when integrate reads unanalyzed
# signals. Prevents reasoning about "current screen activity" based on
# a 10-minute-old frame even if the Swift defer logic misbehaves.
MAX_SCREENSHOT_AGE_SECONDS = 1800  # 30 minutes — OCR text is verbatim
# and doesn't degrade with age like FastVLM descriptions did


def filter_stale_screenshots(
    signal_items: list[dict],
    max_age_seconds: int = MAX_SCREENSHOT_AGE_SECONDS,
) -> list[dict]:
    """Drop screenshot signals older than ``max_age_seconds``. Pure.

    Observation timestamps are NAIVE LOCAL — every collector writes
    ``datetime.now()`` or ``datetime.strptime(...)`` with no tzinfo.
    Comparing against naive-local ``now`` keeps the arithmetic inside
    a single timezone. A prior version stamped the naive timestamp
    with ``tzinfo=timezone.utc`` before subtracting from an aware-UTC
    ``now``, which is WRONG — it doesn't convert local to UTC, it
    just declares the local wall clock to be UTC. On a system running
    at UTC-7 that made a 30-second-old screenshot look 7h 0m 30s old
    and silently dropped it. Integrate was flying blind on current
    screen context for every non-UTC user.

    Invariant: must keep a signal whose timestamp equals ``datetime.now()``
    at call time. Regression-tested in
    ``tests/test_stale_screenshot_filter.py``.
    """
    if not signal_items:
        return []
    now_naive = datetime.now()
    kept: list[dict] = []
    for item in signal_items:
        if item.get("source") != "screenshot":
            kept.append(item)
            continue
        ts_str = item.get("timestamp") or ""
        try:
            ts_dt = datetime.fromisoformat(ts_str)
            if ts_dt.tzinfo is not None:
                ts_dt = ts_dt.astimezone().replace(tzinfo=None)
            age = (now_naive - ts_dt).total_seconds()
        except Exception:
            # Unparseable timestamp — keep the signal rather than
            # silently dropping it; the integrator will surface any
            # downstream failures.
            kept.append(item)
            continue
        if age > max_age_seconds:
            log.info("Dropped stale screenshot signal: %ds old", int(age))
            continue
        kept.append(item)
    return kept

# Module-level mutex + active-loop reference so user-initiated triggers
# can fire run_analysis_cycle immediately without stomping the scheduler.
# Set by AgentLoop.run() (in-process callers like the mic/command
# endpoints bypass this if web and monitor run in separate processes —
# see _TRIGGER_FILE for the cross-process fallback).
_active_loop: "AgentLoop | None" = None  # noqa: F821 — forward ref
_cycle_lock: asyncio.Lock = asyncio.Lock()

# Cross-process trigger marker. The web process writes this file when
# mic_routes or command_routes want to force an immediate integrate;
# the monitor process (which owns AgentLoop) picks it up at the top of
# each analysis iteration via ``consume_trigger()``.
_TRIGGER_FILE = DEJA_HOME / "integrate_trigger.json"


def set_active_loop(loop_ref) -> None:
    """Register the running AgentLoop so in-process triggers can use it."""
    global _active_loop
    _active_loop = loop_ref


def clear_active_loop() -> None:
    global _active_loop
    _active_loop = None


def consume_trigger() -> dict | None:
    """Pop the cross-process integrate trigger if present. Monitor-side.

    Returns the trigger payload (``{reason}``) or None.
    Deletes the file so the next cycle doesn't re-trigger.
    """
    try:
        if not _TRIGGER_FILE.exists():
            return None
        raw = _TRIGGER_FILE.read_text(encoding="utf-8")
        _TRIGGER_FILE.unlink(missing_ok=True)
        if not raw.strip():
            return {"reason": "file_empty"}
        return json.loads(raw)
    except Exception:
        log.exception("consume_trigger: failed to read/parse trigger file")
        try:
            _TRIGGER_FILE.unlink(missing_ok=True)
        except Exception:
            pass
        return None


async def trigger_integrate_now(reason: str, trigger_kind: str = "user_cmd") -> None:
    """Fire run_analysis_cycle immediately against the active agent loop.

    Called from mic_routes (after voice transcript finalization) and
    from command_routes (after context-type commands).

    Uses a module-level mutex so overlapping triggers don't stomp each
    other — if a cycle is already in progress, the new trigger is a
    no-op and the scheduler's next tick picks up any additional signals.

    If no AgentLoop is registered in this process (web and monitor run
    as separate processes), drops a JSON marker at
    ``~/.deja/integrate_trigger.json`` so the monitor process's analysis
    loop can pick it up on its next iteration.
    """
    loop_ref = _active_loop
    if loop_ref is None:
        # Cross-process path: write a trigger marker the monitor polls.
        try:
            DEJA_HOME.mkdir(parents=True, exist_ok=True)
            _TRIGGER_FILE.write_text(
                json.dumps(
                    {
                        "reason": reason,
                        "ts": datetime.now(timezone.utc).isoformat(),
                    }
                )
            )
            log.info(
                "trigger_integrate_now: no in-process loop, wrote marker %s",
                _TRIGGER_FILE.name,
            )
        except Exception:
            log.exception("trigger_integrate_now: failed to write marker")
        return

    if _cycle_lock.locked():
        log.debug(
            "trigger_integrate_now: cycle already running (%s) — skipping",
            reason,
        )
        return

    async with _cycle_lock:
        log.info(
            "trigger_integrate_now: running immediate cycle (reason=%s)",
            reason,
        )
        try:
            await run_analysis_cycle(
                loop_ref, trigger_kind=trigger_kind, trigger_detail=reason
            )
        except Exception:
            log.exception("trigger_integrate_now: cycle failed")


async def run_analysis_cycle(
    loop_ref,
    trigger_kind: str = "signal",
    trigger_detail: str = "scheduled tick",
) -> None:
    """Wrap the cycle body in a request_scope so every downstream log,
    audit entry, and LLM call shares one correlation id.
    """
    with request_scope() as req_id:
        log.debug("analysis cycle starting (req=%s)", req_id)
        return await _run_analysis_cycle_body(
            loop_ref, trigger_kind=trigger_kind, trigger_detail=trigger_detail
        )


async def _run_analysis_cycle_body(
    loop_ref,
    trigger_kind: str = "signal",
    trigger_detail: str = "scheduled tick",
) -> None:
    """One cycle: read signals -> triage -> LLM call -> apply wiki updates.

    ``loop_ref`` is the AgentLoop instance (used for gemini client,
    collector, wiki lock, stats counters, and the stats callback).

    Message-type signals (imessage / whatsapp / email) are first run
    through a cheap local-model triage call that reads the wiki index
    and decides whether they're worth waking up Flash. Non-message
    signals (calendar, tasks, drive, screenshot, clipboard) pass
    through untouched. If everything in the batch is dropped, we skip
    the Flash call entirely.

    All kept signals run through ONE combined integrate call — no
    split between messages and context — so the model can correlate
    across modalities (e.g. a voice note + a screenshot + an email
    arriving in the same window get reasoned about together). The
    5-minute cadence keeps batches small enough that attention isn't
    diluted.

    **Catch-up nightly:** before the regular analysis work, we
    check whether the nightly Pro pass has run since the most
    recent 02:00 local-time threshold. If not, we run nightly
    FIRST and return — deferring this cycle's Flash analysis
    until next iteration.
    """
    # 0. Nightly catch-up check — runs before anything else so an
    #    overdue nightly can't be starved by a busy analysis queue.
    if await loop_ref._maybe_run_catchup_nightly():
        return

    loop_ref.phase = "THINKING"

    # Register cycle context so every downstream audit.record() call
    # (wiki writes, goal ops, action execution) carries cycle + trigger
    # provenance automatically, without threading the ids through every
    # function signature.
    cycle_id = audit.new_cycle_id()
    audit.set_context(cycle_id, trigger_kind, trigger_detail)

    # 1. Read unanalyzed signals as structured dicts (so we can triage
    #    per-signal before formatting the prompt).
    loop = asyncio.get_running_loop()
    signal_items, analysis_marker = await loop.run_in_executor(
        None, loop_ref.collector.get_unanalyzed_signals_structured
    )

    # 1.0. Drop stale screenshot signals. Vision context is only
    #      useful if it reflects the user's CURRENT screen.
    if signal_items:
        signal_items = filter_stale_screenshots(signal_items)

    if not signal_items:
        log.info("No recent signals for analysis")
        if analysis_marker:
            loop_ref.collector.save_analysis_marker(analysis_marker)
        loop_ref.phase = "IDLE"
        return

    # 1a. Deterministic triage: drop automation / off-catalog noise,
    #     always keep Tier 1 (user-authored / inner-circle) and
    #     Tier 2 (focused-attention screenshots).
    kept_items = triage_signals(signal_items)

    if not kept_items:
        log.info(
            "All %d signals triaged away -- skipping Flash cycle",
            len(signal_items),
        )
        if analysis_marker:
            loop_ref.collector.save_analysis_marker(analysis_marker)
        loop_ref.phase = "IDLE"
        return

    if len(kept_items) < len(signal_items):
        log.info(
            "Triage kept %d / %d signals for Flash",
            len(kept_items),
            len(signal_items),
        )

    # Register the exact signal id_keys that seed this cycle so every
    # audit.record() downstream carries `signal_ids` — annotation tools
    # join writes to their real inputs instead of guessing by time window.
    audit.set_signals([i.get("id_key") for i in kept_items if i.get("id_key")])

    # Graphiti ingest moved to observation_cycle (real-time, per-signal).
    # No longer batched here — each signal fires add_episode() the moment
    # it's collected, not 5 min later when analysis runs.

    # 1b. Capture the window list ONCE for the whole cycle — this goes
    #     into the integrate prompt as context, not into each signal.
    open_windows_text = ""
    try:
        from deja.ax_context import capture_all_windows

        all_windows = capture_all_windows()
        if all_windows:
            open_windows_text = "\n".join(
                f"- {w['app']}: {w['title']}" for w in all_windows[:20]
            )
    except Exception:
        log.debug("capture_all_windows failed", exc_info=True)

    # 2. Rebuild the wiki index so any out-of-band changes (manual
    #    deletes, Obsidian edits, git ops) are reflected before we
    #    retrieve.
    try:
        from deja.wiki_catalog import rebuild_index
        rebuild_index()
    except Exception:
        log.debug("index rebuild failed", exc_info=True)

    # 2a. Build focused wiki context via QMD retrieval instead of
    #     dumping the whole wiki.
    from deja.wiki_retriever import build_analysis_context
    try:
        wiki_text = build_analysis_context(kept_items)
    except Exception:
        log.exception("wiki_retrieval failed -- falling back to full wiki")
        wiki_text = wiki_store.render_for_prompt()

    # 3. Run integrate — all kept signals as one combined batch so the
    #    LLM can correlate across modalities. The 5-minute cadence keeps
    #    batches small enough that attention isn't diluted.
    all_wiki_updates: list[dict] = []
    all_reasoning: list[str] = []
    all_goal_actions: list[dict] = []
    all_tasks_updates: list[dict] = []
    all_narratives: list[str] = []

    batches_to_run: list[tuple[str, list[dict]]] = [("combined", kept_items)]

    for batch_name, batch_items in batches_to_run:
        if not batch_items:
            continue
        batch_text = format_signals(batch_items)

        # Build a second timeline with raw Apple Vision OCR swapped in
        # for screenshot observations (preprocessed text otherwise).
        # Only the Claude shadow consumes this — Gemini production and
        # the "apples-to-apples" claude-local shadow both use
        # preprocessed. See signals/format._with_raw_ocr + llm_client
        # integrate_observations for how it's routed. Cheap to build
        # regardless of whether the shadow flag is on; the extra
        # sidecar reads are bounded by screenshot count per batch.
        try:
            from deja.config import INTEGRATE_CLAUDE_SHADOW
            claude_override = (
                format_signals(batch_items, use_raw_ocr=True)
                if INTEGRATE_CLAUDE_SHADOW else None
            )
        except Exception:
            claude_override = None

        log.info("Running analysis on %d %s...", len(batch_items), batch_name)

        # Save fixture for local model evaluation
        try:
            from deja.config import DEJA_HOME
            import json as _json
            fixture_dir = DEJA_HOME / "integration_fixtures"
            fixture_dir.mkdir(exist_ok=True)
            from datetime import datetime as _dt
            ts = _dt.now().strftime("%Y%m%d-%H%M%S")
            fixture_path = fixture_dir / f"{ts}-{batch_name}.json"
            fixture_path.write_text(_json.dumps({
                "batch_name": batch_name,
                "signals_text": batch_text,
                "wiki_text": wiki_text[:5000],  # truncate for storage
                "timestamp": ts,
            }, indent=2))
            log.debug("Saved integration fixture: %s", fixture_path.name)
        except Exception:
            pass

        try:
            result = await loop_ref.gemini.integrate_observations(
                signals_text=batch_text,
                wiki_text=wiki_text,
                open_windows=open_windows_text,
                claude_signals_text_override=claude_override,
                claude_signal_items=batch_items,
            )

            # Save the response alongside the fixture
            try:
                response_path = fixture_dir / f"{ts}-{batch_name}-response.json"
                response_path.write_text(_json.dumps(result, indent=2, default=str))
            except Exception:
                pass

            updates = result.get("wiki_updates", [])
            reasoning = result.get("reasoning", "")
            all_wiki_updates.extend(updates)
            all_goal_actions.extend(result.get("goal_actions") or [])
            if result.get("tasks_update"):
                all_tasks_updates.append(result["tasks_update"])
            if reasoning:
                all_reasoning.append(reasoning)
                log.info("Reasoning (%s): %s", batch_name, reasoning[:200])
            narrative = (result.get("observation_narrative") or "").strip()
            if narrative:
                all_narratives.append(narrative)
            # First successful batch resets the consecutive-failure counter
            # so a long run of transients is followed by a clean slate.
            global _consecutive_integrate_failures
            _consecutive_integrate_failures = 0
        except DejaError as err:
            # Typed failure from llm_client — classify and report with
            # escalation: silent for the first few transients, toast
            # after the threshold.
            _consecutive_integrate_failures += 1
            visible = _consecutive_integrate_failures >= _CONSECUTIVE_FAIL_THRESHOLD
            err.details.setdefault("batch", batch_name)
            err.details.setdefault(
                "consecutive_failures", _consecutive_integrate_failures
            )
            report_error(err, visible_to_user=visible)
        except Exception as e:
            # Untyped failure — wrap so it still flows through the
            # two-sink reporter. Integrate is internal; keep invisible
            # unless it's been failing repeatedly.
            _consecutive_integrate_failures += 1
            visible = _consecutive_integrate_failures >= _CONSECUTIVE_FAIL_THRESHOLD
            wrapped = LLMError(
                f"integrate {batch_name} batch failed: {type(e).__name__}: {e}",
                details={
                    "batch": batch_name,
                    "exception_type": type(e).__name__,
                    "consecutive_failures": _consecutive_integrate_failures,
                },
            )
            log.exception("integrate %s batch failed", batch_name)
            report_error(wrapped, visible_to_user=visible)

    wiki_updates = all_wiki_updates
    reasoning = " | ".join(all_reasoning)
    log.info("Cycle: %d wiki updates total", len(wiki_updates))

    # Append observation narratives to ~/Deja/observations/YYYY-MM-DD.md
    # so the user can skim the quality of what Deja is noticing,
    # independent of whether any wiki update fired this cycle.
    #
    # Run the narrative through linkify_body so entity mentions
    # ("Jon Sturos", "Dominique") become [[jon-sturos|Jon Sturos]]
    # links. Integrate's prompt rule ("wrap entity names in [[slug]]")
    # applies to entity prose and event bodies; narratives skip that
    # step in the model, and wiki_linkify's regular sweep only covers
    # people/ and projects/ pages. Post-processing here is the
    # deterministic bridge — the narrative becomes navigable in
    # Obsidian without asking the model to track the slug catalog.
    if all_narratives:
        try:
            from deja.config import WIKI_DIR
            from deja.wiki_linkify import build_catalog, linkify_body

            catalog = build_catalog()
            linked: list[str] = []
            for nar in all_narratives:
                try:
                    new_body, _ = linkify_body(nar, catalog)
                    linked.append(new_body)
                except Exception:
                    linked.append(nar)

            now_local = datetime.now()
            obs_dir = WIKI_DIR / "observations"
            obs_dir.mkdir(parents=True, exist_ok=True)
            obs_file = obs_dir / f"{now_local.strftime('%Y-%m-%d')}.md"
            header = now_local.strftime("## %H:%M:%S")
            body = "\n\n".join(linked)
            entry = f"{header}\n\n{body}\n\n---\n\n"
            with obs_file.open("a", encoding="utf-8") as f:
                f.write(entry)
        except Exception:
            log.debug("observation_narrative write failed", exc_info=True)

    # 4. Apply (guarded by the shared wiki lock so a concurrent
    #    first-run onboarding backfill can't stomp these writes).
    loop_ref.phase = "RECORDING"
    async with loop_ref._wiki_lock:
        applied = await asyncio.get_running_loop().run_in_executor(
            None, lambda: wiki_store.apply_updates(wiki_updates)
        )
    loop_ref.matches_found += applied

    # 4a. Execute goal_actions
    if all_goal_actions:
        try:
            from deja.goal_actions import execute_all
            actions_done = execute_all(all_goal_actions)
            if actions_done:
                log.info("Cycle: executed %d goal action(s)", actions_done)
        except Exception:
            log.exception("goal_actions execution failed")

    # 4b. Update goals.md
    for tu in all_tasks_updates:
        try:
            from deja.goals import apply_tasks_update
            changes = apply_tasks_update(tu)
            if changes:
                log.info("Cycle: updated %d item(s) in goals.md", changes)
        except Exception:
            log.exception("goals tasks_update failed")

    # Cycle-level summary — one audit entry even when nothing changed,
    # so "why did nothing happen on cycle c_X?" is answerable.
    if not wiki_updates and not all_tasks_updates and not all_goal_actions:
        audit.record(
            "cycle_no_op",
            target=f"cycle/{cycle_id}",
            reason=(reasoning[:200] if reasoning else "no updates"),
        )

    if analysis_marker:
        loop_ref.collector.save_analysis_marker(analysis_marker)

    # Business-intelligence telemetry — one event per completed cycle
    # with the counts the admin dashboard needs to measure engagement
    # (cycles/day/user) + cost (via tasks_updates + wiki_updates as
    # proxy for integrate load). No content, just counts.
    try:
        from deja.telemetry import track

        track("cycle_completed", {
            "cycle_id": cycle_id,
            "trigger_kind": trigger_kind,
            "signal_count": len(signal_items),
            "kept_signal_count": len(kept_items),
            "wiki_updates": len(wiki_updates),
            "goal_actions": len(all_goal_actions),
            "tasks_updates": sum(1 for u in all_tasks_updates),
        })
    except Exception:
        log.debug("cycle telemetry failed", exc_info=True)

    # Fire webhooks for any configured receivers (Claude Code Routines,
    # Slack, etc). Non-blocking — runs on a daemon thread. Silent cycles
    # (no wiki changes, no goal changes, no due reminders, no T1 signals)
    # are skipped by the emitter itself. See deja/webhooks.py.
    try:
        from deja.webhooks import emit_cycle_complete
        from deja.goals import due_reminder_topics
        from datetime import date as _date

        # Count T1 signals in this cycle. classify_tier was already run
        # during triage — keep the logic consistent here so we don't
        # drift between the two code paths.
        from deja.signals.tiering import classify_tier
        t1_count = sum(1 for s in signal_items if classify_tier(s) == 1)

        # Today's due reminders — "today or earlier" is the usual gate
        # for "act on this now" reminders.
        try:
            due = due_reminder_topics(_date.today())
        except Exception:
            due = []

        # Merge the batched tasks_updates so the webhook sees the union
        # of everything this cycle mutated.
        merged_tasks_update: dict = {}
        for tu in all_tasks_updates:
            for k, v in (tu or {}).items():
                merged_tasks_update.setdefault(k, [])
                if isinstance(v, list):
                    merged_tasks_update[k].extend(v)

        emit_cycle_complete(
            cycle_id=cycle_id,
            narrative=" ".join(all_narratives),
            wiki_updates=wiki_updates,
            tasks_update=merged_tasks_update,
            due_reminders=due,
            new_t1_signal_count=t1_count,
        )

        # Chief-of-staff reflex: spawn a local `claude` with Deja MCP
        # to decide whether to ping the user or take any action. Gated
        # on the same substantive-cycle criteria as the webhook, plus
        # the user-managed enabled flag at ~/.deja/chief_of_staff/enabled.
        substantive = (
            bool(wiki_updates)
            or any(merged_tasks_update.get(k) for k in merged_tasks_update)
            or bool(due)
            or t1_count > 0
        )
        if substantive:
            from deja.chief_of_staff import invoke as cos_invoke
            cos_invoke(
                cycle_id=cycle_id,
                narrative=" ".join(all_narratives),
                wiki_updates=wiki_updates,
                tasks_update=merged_tasks_update,
                due_reminders=due,
                new_t1_signal_count=t1_count,
            )
    except Exception:
        log.debug("cycle webhook emit failed", exc_info=True)

    loop_ref.last_analysis_time = datetime.now(timezone.utc)
    loop_ref._fire_stats_update()
    loop_ref.phase = "IDLE"
    audit.clear_context()


# ---------------------------------------------------------------------------
# Triage / formatting — re-exported from ``deja.signals`` so existing
# imports (and any tests that patched these names here) keep working.
# The real implementations now live under ``src/deja/signals/``.
# ---------------------------------------------------------------------------

from deja.signals import format_signals, triage_signals  # noqa: E402,F401
