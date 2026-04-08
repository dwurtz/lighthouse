"""AgentLoop — signal collection + periodic analysis.

Runs headless behind the notch app. Collects signals from all configured
sources into observations.jsonl, then every few minutes reads the fresh
signals and runs one LLM call that merges them into the wiki.

This module is the thin orchestrator. Actual work is delegated to:
  - observation_cycle.py  (signal collection)
  - analysis_cycle.py     (triage + Flash integration)
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from datetime import datetime, timezone
from typing import Any, Callable

from deja.config import (
    INTEGRATE_INTERVAL,
    OBSERVE_INTERVAL,
)
from deja.llm_client import GeminiClient
from deja.observations.collector import Observer

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
    quietly if no email is configured.
    """
    import base64
    from deja.identity import load_user

    user_email = load_user().email
    if not user_email:
        log.warning("_send_email skipped: no user email in self-page frontmatter")
        return

    raw_msg = (
        f"From: {user_email}\n"
        f"To: {user_email}\n"
        f"Subject: [deja] {subject}\n\n{body}"
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
        self._wiki_lock: asyncio.Lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Start the signal and analysis loops as concurrent tasks."""
        self.running = True
        log.info("Monitor loop starting")

        # Startup health checks
        try:
            from deja.health_check import report_health_checks
            self._startup_failures = report_health_checks()
        except Exception:
            log.exception("startup_check failed")
            self._startup_failures = set()

        # Build the in-memory contact index (AddressBook SQLite) on startup
        try:
            from deja.observations.contacts import _build_index
            _build_index()
        except Exception:
            log.exception("Contacts index build failed")

        # Ensure the wiki is a git repo so every cycle can auto-commit
        try:
            from deja.wiki_git import ensure_repo
            ensure_repo()
        except Exception:
            log.exception("wiki_git.ensure_repo failed")

        # Nightly catch-up on startup
        try:
            from deja.reflection import should_run_reflection
            if should_run_reflection():
                log.info("Startup catch-up: nightly is overdue, scheduling")
                asyncio.create_task(self._maybe_run_catchup_nightly())
        except Exception:
            log.exception("startup catch-up check failed")

        # First-run onboarding
        try:
            from deja.onboarding import ALL_STEPS, is_step_done
            from deja.identity import load_user
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
                        from deja.activity_log import append_log_entry
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

        # Meeting coordinator
        try:
            from deja.meeting_coordinator import meeting_poll_loop
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
        from deja.agent.observation_cycle import run_collect_cycle

        while self.running:
            try:
                await run_collect_cycle(self)
            except Exception:
                log.exception("Error in signal collection cycle")
            await asyncio.sleep(OBSERVE_INTERVAL)

    async def _analysis_loop(self) -> None:
        """Run analysis every INTEGRATE_INTERVAL seconds."""
        from deja.agent.analysis_cycle import run_analysis_cycle

        await asyncio.sleep(30)  # short delay to collect initial signals
        while self.running:
            try:
                await run_analysis_cycle(self)
            except Exception:
                log.exception("Error in analysis cycle")
            await asyncio.sleep(INTEGRATE_INTERVAL)

    # ------------------------------------------------------------------
    # Delegated cycle methods (kept as instance methods for backward compat)
    # ------------------------------------------------------------------

    async def _collect_cycle(self) -> None:
        """One collection cycle -- delegates to observation_cycle module."""
        from deja.agent.observation_cycle import run_collect_cycle
        await run_collect_cycle(self)

    async def _analysis_cycle(self) -> None:
        """One analysis cycle -- delegates to analysis_cycle module."""
        from deja.agent.analysis_cycle import run_analysis_cycle
        await run_analysis_cycle(self)

    @staticmethod
    def _format_signals(items: list[dict]) -> str:
        """Format structured signal dicts — delegates to analysis_cycle module."""
        from deja.agent.analysis_cycle import format_signals
        return format_signals(items)

    async def _triage_signals(self, items: list[dict]) -> list[dict]:
        """Triage signals — delegates to analysis_cycle module."""
        from deja.agent.analysis_cycle import triage_signals
        return await triage_signals(items)

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
        """Background task wrapper for first-run onboarding."""
        try:
            from deja.onboarding import run_all_pending_steps
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
        """Run nightly inline if it hasn't run since today's slot. Returns True if it ran."""
        from deja.reflection import should_run_reflection, run_reflection

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
