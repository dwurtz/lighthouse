"""Analysis (integrate) cycle — extracted from AgentLoop.

Reads unanalyzed signals, triages them, runs Flash integration,
applies wiki updates, and logs results.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

from deja import wiki as wiki_store
from deja.agent.integration import log_analysis

log = logging.getLogger(__name__)


async def run_analysis_cycle(loop_ref) -> None:
    """One cycle: read signals -> triage -> LLM call -> apply wiki updates.

    ``loop_ref`` is the AgentLoop instance (used for gemini client,
    collector, wiki lock, stats counters, and the stats callback).

    Message-type signals (imessage / whatsapp / email) are first run
    through a cheap local-model triage call that reads the wiki index
    and decides whether they're worth waking up Flash. Non-message
    signals (calendar, tasks, drive, screenshot, clipboard) pass
    through untouched. If everything in the batch is dropped, we skip
    the Flash call entirely.

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

    # 1. Read unanalyzed signals as structured dicts (so we can triage
    #    per-signal before formatting the prompt).
    loop = asyncio.get_running_loop()
    signal_items, analysis_marker = await loop.run_in_executor(
        None, loop_ref.collector.get_unanalyzed_signals_structured
    )

    if not signal_items:
        log.info("No recent signals for analysis")
        if analysis_marker:
            loop_ref.collector.save_analysis_marker(analysis_marker)
        loop_ref.phase = "IDLE"
        return

    # 1a. Triage message-type signals via local Gemma.
    kept_items = await triage_signals(signal_items)

    if not kept_items:
        log.info(
            "All %d signals triaged away -- skipping Flash cycle",
            len(signal_items),
        )
        if analysis_marker:
            loop_ref.collector.save_analysis_marker(analysis_marker)
        loop_ref.phase = "IDLE"
        return

    # Split into message-type (conversations, high-detail events) and
    # context-type (screenshots, browser, clipboard — ambient context).
    _MESSAGE_SOURCES = {"imessage", "whatsapp", "email", "chat", "microphone"}
    message_items = [d for d in kept_items if d.get("source") in _MESSAGE_SOURCES]
    context_items = [d for d in kept_items if d.get("source") not in _MESSAGE_SOURCES]

    if len(kept_items) < len(signal_items):
        log.info(
            "Triage kept %d / %d signals for Flash (%d messages, %d context)",
            len(kept_items),
            len(signal_items),
            len(message_items),
            len(context_items),
        )

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

    # 3. Run integrate — message batch and context batch separately.
    all_wiki_updates: list[dict] = []
    all_reasoning: list[str] = []
    all_goal_actions: list[dict] = []
    all_tasks_updates: list[dict] = []

    for batch_name, batch_items in [("messages", message_items), ("context", context_items)]:
        if not batch_items:
            continue
        batch_text = format_signals(batch_items)
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
        except Exception:
            log.exception("integrate %s batch failed", batch_name)

    wiki_updates = all_wiki_updates
    reasoning = " | ".join(all_reasoning)
    log.info("Cycle: %d wiki updates total", len(wiki_updates))

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

    # Human-readable log in the wiki (browse in Obsidian).
    try:
        from deja.activity_log import append_log_entry
        if wiki_updates:
            changed = ", ".join(f"{u.get('category', '?')}/{u.get('slug', '?')}" for u in wiki_updates[:5])
            append_log_entry("cycle", f"Updated {len(wiki_updates)} page(s): {changed}")
        else:
            summary = (reasoning[:140] + "...") if len(reasoning) > 140 else (reasoning or "no updates")
            append_log_entry("cycle", f"No updates — {summary}")
    except Exception:
        log.warning("Failed to append cycle entry to log.md", exc_info=True)

    # 5. Audit log — diagnostic only; rendered by the notch Activity tab
    log_analysis(
        matches=[
            {
                "goal": f"{u.get('category', '')}/{u.get('slug', '')}",
                "signal_summary": u.get("reason", ""),
                "confidence": u.get("action", "update"),
                "reasoning": "",
            }
            for u in wiki_updates
        ],
        skips=[],
        new_facts=[],
        commitments=[],
        events=[],
        proposed_goals=[],
        conversations=[{"with": "wiki", "summary": reasoning}] if reasoning else [],
        questions=[],
    )

    if analysis_marker:
        loop_ref.collector.save_analysis_marker(analysis_marker)

    loop_ref.last_analysis_time = datetime.now(timezone.utc)
    loop_ref._fire_stats_update()
    loop_ref.phase = "IDLE"


# ---------------------------------------------------------------------------
# Triage / formatting helpers
# ---------------------------------------------------------------------------

def format_signals(items: list[dict]) -> str:
    """Format structured signal dicts the same way the old text reader did.

    Matches Observer.get_unanalyzed_signals_from_log so the Flash
    prompt looks identical to before.
    """
    lines: list[str] = []
    for d in items:
        ts = d.get("timestamp", "")
        source = d.get("source", "?")
        sender = d.get("sender", "?")
        text = (d.get("text", "") or "")[:400]
        lines.append(f"[{ts}] [{source}] {sender}: {text}")
    if len(lines) > 200:
        older_count = len(lines) - 200
        lines = [f"({older_count} older signals omitted)"] + lines[-200:]
    return "\n".join(lines)


async def triage_signals(items: list[dict]) -> list[dict]:
    """Filter message-type signals through one batched Flash-Lite call.

    Non-message signals (calendar, drive, tasks, screenshot, clipboard,
    microphone) pass through untouched. Recall-biased — on any failure
    every triaged signal is kept.

    **Outbound messages bypass triage entirely.** Anything David wrote
    himself (imessage/whatsapp sent by "You", email from his address)
    is intent-laden by definition — commitments, decisions, questions
    he's asking — and is never worth dropping. Only inbound messages
    get triaged for noise.
    """
    from deja.llm import prefilter as local_llm
    from deja.observations.types import is_outbound

    # Partition: inbound message signals get triaged. Everything
    # else (non-message signals AND any outbound message) passes
    # through untouched.
    to_triage: list[tuple[int, dict]] = []
    passthrough: list[tuple[int, dict]] = []
    for i, d in enumerate(items):
        if d.get("source") in local_llm.TRIAGE_SOURCES and not is_outbound(d):
            to_triage.append((i, d))
        else:
            passthrough.append((i, d))

    if not to_triage:
        return items

    # One batched Flash-Lite call for the whole cycle's message signals.
    index_md = local_llm.load_index_md()
    triage_items = [d for _, d in to_triage]
    try:
        verdicts = await local_llm.triage_batch(triage_items, index_md=index_md)
    except Exception:
        log.exception("Triage batch call failed — keeping all signals")
        verdicts = [(True, "triage exception — keeping")] * len(triage_items)

    kept: list[tuple[int, dict]] = list(passthrough)
    for (i, d), (relevant, reason) in zip(to_triage, verdicts):
        if relevant:
            kept.append((i, d))
        else:
            log.info(
                "Triage dropped [%s] %s: %s",
                d.get("source", "?"),
                d.get("sender", "?"),
                reason,
            )

    kept.sort(key=lambda pair: pair[0])
    return [d for _, d in kept]
