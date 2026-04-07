"""AgentLoop — signal collection + periodic analysis.

Runs headless behind the notch app. Collects signals from all configured
sources into observations.jsonl, then every few minutes reads the fresh
signals and runs one LLM call that merges them into the wiki.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
from datetime import datetime, timezone
from typing import Any, Callable

from lighthouse.config import (
    INTEGRATE_INTERVAL,
    OBSERVE_INTERVAL,
    LIGHTHOUSE_HOME,
)
from lighthouse.llm_client import GeminiClient
from lighthouse.agent.integration import log_analysis
from lighthouse.observations.collector import Observer
from lighthouse.observations.types import Observation
from lighthouse import wiki as wiki_store

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_json(raw: str) -> Any:
    """Best-effort JSON extraction from LLM output."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    for open_ch, close_ch in [("[", "]"), ("{", "}")]:
        if open_ch in text and close_ch in text:
            start = text.index(open_ch)
            end = text.rindex(close_ch) + 1
            try:
                return json.loads(text[start:end])
            except json.JSONDecodeError:
                continue
    return json.loads(text)


def _send_email(subject: str, body: str) -> None:
    """Send an email to the user themselves via gws gmail.

    Pulls From/To from the user profile (self-page frontmatter). No-ops
    quietly if no email is configured — a missing identity shouldn't crash
    a monitor cycle.
    """
    import base64
    from lighthouse.identity import load_user

    user_email = load_user().email
    if not user_email:
        log.warning("_send_email skipped: no user email in self-page frontmatter")
        return

    raw_msg = (
        f"From: {user_email}\n"
        f"To: {user_email}\n"
        f"Subject: [lighthouse] {subject}\n\n{body}"
    )
    encoded = base64.urlsafe_b64encode(raw_msg.encode()).decode()
    try:
        subprocess.run(
            [
                "gws", "gmail", "users", "messages", "send",
                "--params", json.dumps({"userId": "me"}),
                "--json", json.dumps({"raw": encoded}),
            ],
            capture_output=True,
            timeout=15,
        )
    except Exception:
        log.exception("Email send failed")


# ---------------------------------------------------------------------------
# AgentLoop
# ---------------------------------------------------------------------------

class AgentLoop:
    """Signal collection + periodic analysis loop."""

    def __init__(
        self,
        gemini: GeminiClient,
        collector: Observer,
    ) -> None:
        self.gemini = gemini
        self.collector = collector
        self.running = False

        # Stats
        self.signals_collected = 0
        self.matches_found = 0
        self.last_signal_time: datetime | None = None
        self.last_analysis_time: datetime | None = None
        self.phase: str = "IDLE"

        # Tray-app callback: called with (signal_count, match_count) after updates
        self.on_stats_update: Callable[[int, int], None] | None = None

        # Shared wiki-write lock. The analysis cycle writes via
        # wiki_store.apply_updates; during first-run onboarding the
        # background backfill is also writing via the same function.
        # Both paths acquire this lock so we never have two concurrent
        # rewrites of the same slug stomping each other.
        self._wiki_lock: asyncio.Lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Start the signal and analysis loops as concurrent tasks."""
        self.running = True
        log.info("Monitor loop starting")

        # Startup health checks — probe every external dependency once and
        # write a visible diagnostic to log.md so David can spot problems
        # in Obsidian instead of scrolling lighthouse.log.
        try:
            from lighthouse.health_check import report_health_checks
            self._startup_failures = report_health_checks()
        except Exception:
            log.exception("startup_check failed")
            self._startup_failures = set()

        # Build the in-memory contact index (AddressBook SQLite) on startup
        try:
            from lighthouse.observations.contacts import _build_index
            _build_index()
        except Exception:
            log.exception("Contacts index build failed")

        # Ensure the wiki is a git repo so every cycle can auto-commit
        try:
            from lighthouse.wiki_git import ensure_repo
            ensure_repo()
        except Exception:
            log.exception("wiki_git.ensure_repo failed")

        # Nightly catch-up on startup — if the last nightly was more
        # than 22h ago (because the process was killed, or macOS was
        # asleep at 02:00 last night, or this is a fresh install),
        # run it immediately as a background task. Doesn't block the
        # signal/analysis loops from starting. The analysis cycle's
        # own catch-up check will see the in-flight lock and yield.
        try:
            from lighthouse.reflection import should_run_reflection
            if should_run_reflection():
                log.info("Startup catch-up: nightly is overdue, scheduling")
                asyncio.create_task(self._maybe_run_catchup_nightly())
        except Exception:
            log.exception("startup catch-up check failed")

        # First-run onboarding: if any onboarding steps haven't completed,
        # kick off the runner in the background. It walks ALL_STEPS
        # (sent email → iMessage → WhatsApp) sequentially, skipping any
        # already done, and runs every wiki write under self._wiki_lock
        # so concurrent analysis cycles can't stomp its writes.
        # Non-blocking — the notch and the analysis loop come up
        # immediately; the wiki fills in over the next few minutes.
        try:
            from lighthouse.onboarding import ALL_STEPS, is_step_done
            from lighthouse.identity import load_user
            pending_steps = [
                (name, desc) for name, desc in ALL_STEPS
                if not is_step_done(name)
            ]
            if pending_steps:
                user = load_user()
                if not user.is_generic and user.email:
                    log.info(
                        "First-run onboarding: scheduling %d pending step(s) "
                        "in background: %s",
                        len(pending_steps),
                        ", ".join(n for n, _ in pending_steps),
                    )
                    try:
                        from lighthouse.activity_log import append_log_entry
                        pending_desc = "; ".join(
                            f"{name} ({desc})" for name, desc in pending_steps
                        )
                        append_log_entry(
                            "onboard",
                            f"starting first-run onboarding in background — "
                            f"{pending_desc}. Wiki will populate over the "
                            f"next few minutes.",
                        )
                    except Exception:
                        log.debug(
                            "onboarding startup log append failed",
                            exc_info=True,
                        )
                    asyncio.create_task(self._run_onboarding_backfill())
                else:
                    log.info(
                        "Skipping first-run onboarding: no self-page with "
                        "email in the wiki yet"
                    )
        except Exception:
            log.exception("first-run onboarding check failed")

        # Meeting coordinator — polls calendar for active/imminent meetings
        # and writes a prompt file that Swift reads to show recording banner.
        try:
            from lighthouse.meeting_coordinator import meeting_poll_loop
            meeting_task = asyncio.create_task(meeting_poll_loop())
        except Exception:
            log.exception("meeting_poll_loop failed to start")
            meeting_task = None

        tasks = [
            asyncio.create_task(self._signal_loop()),
            asyncio.create_task(self._analysis_loop()),
        ]
        if meeting_task:
            tasks.append(meeting_task)

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            log.info("Monitor loop cancelled")
        finally:
            self.running = False

    def stop(self) -> None:
        self.running = False

    # ------------------------------------------------------------------
    # Loops
    # ------------------------------------------------------------------

    async def _signal_loop(self) -> None:
        """Collect signals every OBSERVE_INTERVAL seconds."""
        while self.running:
            try:
                await self._collect_cycle()
            except Exception:
                log.exception("Error in signal collection cycle")
            await asyncio.sleep(OBSERVE_INTERVAL)

    async def _analysis_loop(self) -> None:
        """Run analysis every INTEGRATE_INTERVAL seconds."""
        await asyncio.sleep(30)  # short delay to collect initial signals
        while self.running:
            try:
                await self._analysis_cycle()
            except Exception:
                log.exception("Error in analysis cycle")
            await asyncio.sleep(INTEGRATE_INTERVAL)

    # ------------------------------------------------------------------
    # Collect cycle
    # ------------------------------------------------------------------

    async def _collect_cycle(self) -> None:
        """One collection cycle -- gather all new signals."""
        self.phase = "OBSERVING"

        loop = asyncio.get_running_loop()
        new_signals = await loop.run_in_executor(None, self.collector.collect_all)

        if new_signals:
            for sig in new_signals:
                if sig.source == "screenshot":
                    # Screenshots are the only source that needs post-collection
                    # processing: the collector hands us a raw PNG path, we run
                    # vision on it, replace the placeholder text with the
                    # description, optionally retain a copy for vision eval,
                    # delete the original, and persist.
                    image_path = getattr(sig, "_image_path", None)
                    if image_path:
                        try:
                            vision_result = await self.gemini.describe_screen(image_path)
                            sig.text = (vision_result.get("summary") or "").strip() or "(empty vision description)"
                        finally:
                            # Optional retention for vision A/B eval — gated by
                            # config.VISION_RETENTION. Saves PNG + sidecar .txt
                            # so we can rerun alternate models against real frames.
                            try:
                                from lighthouse.config import VISION_RETENTION, VISION_RETENTION_DIR
                                if VISION_RETENTION:
                                    import shutil
                                    VISION_RETENTION_DIR.mkdir(parents=True, exist_ok=True)
                                    ts = sig.timestamp.strftime("%Y%m%d-%H%M%S")
                                    dest_png = VISION_RETENTION_DIR / f"{ts}.png"
                                    dest_txt = VISION_RETENTION_DIR / f"{ts}.txt"
                                    shutil.copy(image_path, dest_png)
                                    dest_txt.write_text(sig.text, encoding="utf-8")
                            except Exception:
                                log.debug("vision retention failed", exc_info=True)
                            try:
                                os.remove(image_path)
                            except OSError:
                                pass
                    self.collector._persist_signal(sig)

            self.signals_collected += len(new_signals)
            self.last_signal_time = datetime.now(timezone.utc)
            log.info("Collected %d new signals", len(new_signals))
            self._fire_stats_update()

        self.phase = "IDLE"

    # ------------------------------------------------------------------
    # Analysis cycle
    # ------------------------------------------------------------------

    async def _analysis_cycle(self) -> None:
        """One cycle: read signals → triage → one LLM call → apply wiki updates.

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
        until next iteration. Nightly's work is a strict superset of
        an analysis cycle, so skipping one Flash iteration to run it
        is fine. This replaces the old apscheduler 2am cron, which
        silently missed fires when macOS was in maintenance sleep at
        the trigger time.
        """
        # 0. Nightly catch-up check — runs before anything else so an
        #    overdue nightly can't be starved by a busy analysis queue.
        if await self._maybe_run_catchup_nightly():
            # Nightly ran; skip the rest of this cycle. The next
            # scheduled analysis cycle picks up normally from whatever
            # signals arrived while nightly was running.
            return

        self.phase = "THINKING"

        # 1. Read unanalyzed signals as structured dicts (so we can triage
        #    per-signal before formatting the prompt).
        loop = asyncio.get_running_loop()
        signal_items, analysis_marker = await loop.run_in_executor(
            None, self.collector.get_unanalyzed_signals_structured
        )

        if not signal_items:
            log.info("No recent signals for analysis")
            if analysis_marker:
                self.collector.save_analysis_marker(analysis_marker)
            self.phase = "IDLE"
            return

        # 1a. Triage message-type signals via local Gemma.
        kept_items = await self._triage_signals(signal_items)

        if not kept_items:
            log.info(
                "All %d signals triaged away -- skipping Flash cycle",
                len(signal_items),
            )
            if analysis_marker:
                self.collector.save_analysis_marker(analysis_marker)
            self.phase = "IDLE"
            return

        # Split into message-type (conversations, high-detail events) and
        # context-type (screenshots, browser, clipboard — ambient context).
        # Processing them separately ensures conversations get focused
        # attention from the model instead of being diluted by 12 screenshots
        # of code editing. Both batches go through the same integrate prompt.
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
            from lighthouse.wiki_catalog import rebuild_index
            rebuild_index()
        except Exception:
            log.debug("index rebuild failed", exc_info=True)

        # 2a. Build focused wiki context via QMD retrieval instead of
        #     dumping the whole wiki. This is where the big token savings
        #     come from — prompts go from ~full-wiki to index + a handful
        #     of retrieved pages.
        from lighthouse.wiki_retriever import build_analysis_context
        try:
            wiki_text = build_analysis_context(kept_items)
        except Exception:
            log.exception("wiki_retrieval failed -- falling back to full wiki")
            wiki_text = wiki_store.render_for_prompt()

        # 3. Run integrate — message batch and context batch separately.
        #    This ensures conversations get focused model attention instead
        #    of being diluted by ambient screenshots. Both batches use the
        #    same prompt and wiki context.
        all_wiki_updates: list[dict] = []
        all_reasoning: list[str] = []

        all_goal_actions: list[dict] = []
        all_tasks_updates: list[dict] = []

        for batch_name, batch_items in [("messages", message_items), ("context", context_items)]:
            if not batch_items:
                continue
            batch_text = self._format_signals(batch_items)
            log.info("Running analysis on %d %s...", len(batch_items), batch_name)
            try:
                result = await self.gemini.integrate_observations(
                    signals_text=batch_text,
                    wiki_text=wiki_text,
                )
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
        self.phase = "RECORDING"
        async with self._wiki_lock:
            applied = await asyncio.get_running_loop().run_in_executor(
                None, lambda: wiki_store.apply_updates(wiki_updates)
            )
        self.matches_found += applied

        # 4a. Execute goal_actions — real-world operations triggered by
        #      automations in goals.md (calendar events, email drafts, tasks,
        #      notifications). Runs immediately at integrate-time so actions
        #      fire within minutes of the triggering observation.
        if all_goal_actions:
            try:
                from lighthouse.goal_actions import execute_all
                actions_done = execute_all(all_goal_actions)
                if actions_done:
                    log.info("Cycle: executed %d goal action(s)", actions_done)
            except Exception:
                log.exception("goal_actions execution failed")

        # 4b. Update goals.md — add/complete tasks, add/resolve waiting-for.
        #      The agent maintains the task list based on observed commitments.
        for tu in all_tasks_updates:
            try:
                from lighthouse.goals import apply_tasks_update
                changes = apply_tasks_update(tu)
                if changes:
                    log.info("Cycle: updated %d item(s) in goals.md", changes)
            except Exception:
                log.exception("goals tasks_update failed")

        # Human-readable log in the wiki (browse in Obsidian).
        try:
            from lighthouse.activity_log import append_log_entry
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
            self.collector.save_analysis_marker(analysis_marker)

        self.last_analysis_time = datetime.now(timezone.utc)
        self._fire_stats_update()
        self.phase = "IDLE"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _fire_stats_update(self) -> None:
        """Notify the tray app of updated stats."""
        if self.on_stats_update is not None:
            try:
                self.on_stats_update(self.signals_collected, self.matches_found)
            except Exception:
                log.exception("on_stats_update callback failed")

    async def _run_onboarding_backfill(self) -> None:
        """Background task wrapper for first-run onboarding.

        Walks every pending onboarding step (sent email → iMessage →
        WhatsApp) sequentially, passing the shared wiki lock so the
        writes never interleave with the analysis cycle's. Any
        exception is logged but never propagated — onboarding is best
        effort and a failure here shouldn't take down the monitor.
        """
        try:
            from lighthouse.onboarding import run_all_pending_steps
            summaries = await run_all_pending_steps(
                days=30,
                wiki_lock=self._wiki_lock,
                gemini=self.gemini,
            )
            log.info(
                "Background onboarding complete: %d step(s) run — %s",
                len(summaries),
                [s.get("step") for s in summaries],
            )
        except Exception:
            log.exception("Background onboarding backfill failed")

    async def _maybe_run_catchup_nightly(self) -> bool:
        """Run nightly inline if it hasn't run since today's 02:00. Returns True if it ran.

        Called on monitor startup and at the start of every analysis
        cycle. Uses a clock-aligned check (has nightly run since the
        most recent 02:00 wall-clock threshold?) rather than an
        interval timer. That's wake-safe — on startup after an
        overnight sleep, the first heartbeat sees the threshold was
        crossed and runs nightly immediately.

        The underlying `run_reflection` holds an asyncio lock,
        so two adjacent analysis cycles both observing the threshold
        simultaneously is harmless — the second caller sees the lock
        held and skips. On successful completion the
        `last_reflection_run` marker is updated, so subsequent cycles
        won't re-trigger until tomorrow's 02:00 threshold passes.
        """
        from lighthouse.reflection import should_run_reflection, run_reflection

        if not should_run_reflection():
            return False

        log.info("Nightly catch-up triggered — running Pro pass now")
        self.phase = "NIGHTLY"
        try:
            result = await run_reflection()
        except Exception:
            log.exception("Catch-up nightly failed")
            self.phase = "IDLE"
            return False

        if result.get("skipped") == "concurrent":
            # The scheduler cron was already running nightly; we
            # didn't do any work, don't treat this as a completed
            # catch-up — the cron's run will update the marker.
            log.info("Nightly was already running via scheduler; catch-up yielded")
            self.phase = "IDLE"
            return False

        log.info(
            "Nightly catch-up complete: %d updates, %d chars thoughts",
            len(result.get("wiki_updates", []) or []),
            len(result.get("thoughts", "") or ""),
        )
        self.last_analysis_time = datetime.now(timezone.utc)
        self._fire_stats_update()
        self.phase = "IDLE"
        return True

    # ------------------------------------------------------------------
    # Triage / formatting helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _format_signals(items: list[dict]) -> str:
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

    async def _triage_signals(self, items: list[dict]) -> list[dict]:
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
        from lighthouse.llm import prefilter as local_llm
        from lighthouse.observations.types import is_outbound

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
        # Wiki index is transmitted exactly once regardless of batch size.
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
