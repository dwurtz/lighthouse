"""Observer -- orchestrates all signal sources with deduplication."""

from __future__ import annotations

import logging
import json
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

from deja.config import DEJA_HOME
from deja.observations.base import BaseObserver
from deja.observations.browser import BrowserObserver
from deja.observations.calendar import CalendarObserver
from deja.observations.clipboard import ClipboardObserver
from deja.observations.drive import DriveObserver
from deja.observations.email import EmailObserver
from deja.observations.imessage import IMessageObserver
from deja.observations.meet import MeetObserver
from deja.observations.screenshot import capture_screenshot_if_changed, ScreenshotObserver
from deja.observations.tasks import TasksObserver
from deja.observations.typed_content import TypedContentObserver
from deja.observations.types import Observation
from deja.observations.whatsapp import WhatsAppObserver
from deja.signal_health import SourceHealthTracker, source_id_for

log = logging.getLogger(__name__)


class Observer:
    """Collects signals from all sources, deduplicating across calls."""

    def __init__(self, history_size: int = 10000) -> None:
        self._seen_ids: set[str] = set()
        self._screenshot_counter: int = 0
        self._screenshot_every: int = 1
        self._signal_log_path = DEJA_HOME / "observations.jsonl"

        # Per-source health state. Tracks ok/error transitions and
        # emits heartbeats via deja.audit. See deja.signal_health.
        self.health = SourceHealthTracker()

        # Observers that run every cycle
        self._every_cycle_observers: list[BaseObserver] = [
            IMessageObserver(),
            WhatsAppObserver(),
            ClipboardObserver(),
            TypedContentObserver(),
        ]

        # Observers gated by a shared GWS counter (every Nth cycle).
        # Email / Calendar / Drive now use delta APIs (historyId /
        # syncToken / pageToken) so each cycle is O(new-changes) and
        # cheap to run more often. We bumped 5 → 2 (~6s at 3s/cycle)
        # on that basis. Going lower would pay rate-limit cost for the
        # non-delta observers in this bucket (tasks, meet) without
        # clear upside.
        self._gws_every: int = 2
        self._gws_counter: int = 0
        self._gws_observers: list[BaseObserver] = [
            EmailObserver(),
            CalendarObserver(),
            DriveObserver(),
            TasksObserver(),
            MeetObserver(),
        ]

        # Browser observer has its own cadence
        self._browser_every: int = 3  # ~9s at OBSERVE_INTERVAL=3
        self._browser_counter: int = 0
        self._browser_observer: BaseObserver = BrowserObserver(since_minutes=10)

        self.recent_history: deque[Observation] = deque(maxlen=history_size)
        self._load_history()

    def _run_observer(self, obs: BaseObserver, raw: list[Observation]) -> None:
        """Invoke one observer's ``collect()`` with signal-health tracking.

        Wraps the call so exceptions are swallowed (isolate one bad
        source from the rest), and every invocation updates the
        in-memory ``SourceHealthTracker`` — which in turn emits
        ``collector_ok`` recovery/heartbeat rows and ``collector_error``
        rows to ``audit.jsonl``. Observers that aren't first-class
        tracked sources (e.g. Meet) just run without health tracking.
        """
        src_id = source_id_for(obs.name)
        try:
            signals = obs.collect()
        except Exception as e:
            log.exception("%s collector error", obs.name)
            if src_id is not None:
                reason = f"{type(e).__name__}: {str(e)[:160]}"
                self.health.record_error(src_id, reason)
            return
        raw.extend(signals)
        if src_id is not None:
            self.health.record_success(src_id)

    def collect_all(self) -> list[Observation]:
        """
        Run all collectors, deduplicate against previously seen ids,
        return only new signals.
        """
        raw: list[Observation] = []

        # Every-cycle observers (messages, clipboard)
        for obs in self._every_cycle_observers:
            self._run_observer(obs, raw)

        # Browser history — every 3rd cycle (~9s)
        self._browser_counter += 1
        if self._browser_counter >= self._browser_every:
            self._browser_counter = 0
            self._run_observer(self._browser_observer, raw)

        # GWS-gated observers — every 5th cycle
        self._gws_counter += 1
        if self._gws_counter >= self._gws_every:
            self._gws_counter = 0
            for obs in self._gws_observers:
                self._run_observer(obs, raw)

        # Microphone: handled entirely by the web server via
        # /api/mic/start and /api/mic/stop. Nothing to poll here — mic
        # transcripts are written to observations.jsonl directly when the
        # user ends a session.

        # Screenshot — purely periodic (every 2 cycles x 3s = 6s).
        # Gated by config.SCREENSHOT_ENABLED so the user can disable the
        # collector if macOS Screen Recording permission is misbehaving
        # (the only observation source that needs TCC screen-recording).
        # We used to also trigger on app/window change, but that required
        # an osascript call to System Events which fired the macOS
        # Automation prompt. The 6s periodic floor is good enough and
        # avoids the TCC prompt entirely.
        from deja.config import SCREENSHOT_ENABLED
        if SCREENSHOT_ENABLED:
            self._screenshot_counter += 1
            if self._screenshot_counter >= 2:  # 2 cycles x 3s = 6s
                self._screenshot_counter = 0
                # Use ScreenshotObserver.collect() which iterates
                # all displays (screen_1.png, screen_2.png, ...)
                # instead of just latest_screen.png. Each display
                # gets its own perceptual hash and vision pass.
                if not hasattr(self, "_screenshot_observer"):
                    self._screenshot_observer = ScreenshotObserver()
                self._run_observer(self._screenshot_observer, raw)

        # Resolve contact names for message signals
        try:
            from deja.observations.contacts import resolve_contact
            for sig in raw:
                if sig.source in ("imessage", "whatsapp") and sig.sender != "You":
                    resolved = resolve_contact(sig.sender)
                    if resolved:
                        sig.sender = f"{resolved} ({sig.sender})" if "+" in sig.sender else resolved
        except Exception:
            pass

        # Thread conversations — group consecutive messages into threads
        try:
            from deja.observations.threads import thread_signals
            raw = thread_signals(raw)
        except Exception:
            log.exception("Threading failed")

        # Deduplicate
        new_signals: list[Observation] = []
        for sig in raw:
            if sig.id_key not in self._seen_ids:
                self._seen_ids.add(sig.id_key)
                new_signals.append(sig)
                self.recent_history.append(sig)
                # Don't persist screenshot signals here — they contain raw file paths.
                # The monitor loop persists them after vision analysis.
                if sig.source != "screenshot":
                    self._persist_signal(sig)

        return new_signals

    def get_unmatched_history_summary(self, matched_ids: set[str], max_recent: int = 50, max_older: int = 30) -> str:
        """Return a summary of unmatched signals: recent ones in detail, older ones compressed by theme."""
        unmatched = [s for s in self.recent_history if s.id_key not in matched_ids]
        if not unmatched:
            return ""

        now = datetime.now()
        recent: list[Observation] = []   # last hour
        older: list[Observation] = []    # older than 1 hour

        for s in unmatched:
            age = (now - s.timestamp).total_seconds()
            if age < 3600:
                recent.append(s)
            else:
                older.append(s)

        lines: list[str] = []

        # Recent: show detail
        if recent:
            lines.append("RECENT (last hour):")
            for s in recent[-max_recent:]:
                ts = s.timestamp.strftime("%H:%M")
                lines.append(f"  [{ts}] [{s.source}] {s.sender}: {s.text[:100]}")

        # Older: compress — just show source counts and sample texts
        if older:
            lines.append(f"\nOLDER ({len(older)} signals over past sessions):")
            # Group by date
            by_date: dict[str, list[Observation]] = {}
            for s in older[-500:]:  # last 500 older signals
                day = s.timestamp.strftime("%b %d")
                by_date.setdefault(day, []).append(s)
            for day, sigs in sorted(by_date.items()):
                samples = [s.text[:60] for s in sigs[-max_older:]]
                lines.append(f"  {day} ({len(sigs)} signals): {'; '.join(samples[:5])}")

        return "\n".join(lines)

    def _load_history(self) -> None:
        """Load recent signal history from disk on startup."""
        if not self._signal_log_path.exists():
            return
        try:
            with open(self._signal_log_path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                        sig = Observation(
                            source=d["source"],
                            sender=d["sender"],
                            text=d["text"],
                            timestamp=datetime.fromisoformat(d["timestamp"]),
                            id_key=d["id_key"],
                        )
                        self.recent_history.append(sig)
                        self._seen_ids.add(sig.id_key)
                    except (json.JSONDecodeError, KeyError):
                        continue
            log.info("Loaded %d signals from history", len(self.recent_history))
        except Exception:
            log.exception("Failed to load signal history")

    def _persist_signal(self, sig: Observation) -> None:
        """Append a signal to the on-disk log."""
        try:
            DEJA_HOME.mkdir(parents=True, exist_ok=True)
            with open(self._signal_log_path, "a") as f:
                f.write(json.dumps({
                    "source": sig.source,
                    "sender": sig.sender,
                    "text": sig.text,
                    "timestamp": sig.timestamp.isoformat(),
                    "id_key": sig.id_key,
                }) + "\n")
        except Exception:
            log.exception("Failed to persist signal")

    def get_unanalyzed_signals_structured(self) -> tuple[list[dict], int]:
        """Read signals added since last analysis as structured dicts.

        Returns (signal_dicts, new_offset). Each dict has keys:
        source, sender, text, timestamp, id_key. Used by the analysis loop
        when it needs to triage per-signal (e.g. cheap local-model filtering
        of message-type signals) before building the final prompt.
        """
        marker_path = DEJA_HOME / "last_integration_offset"
        offset = 0
        if marker_path.exists():
            try:
                offset = int(marker_path.read_text().strip())
            except (ValueError, OSError):
                offset = 0

        if not self._signal_log_path.exists():
            return [], 0

        file_size = self._signal_log_path.stat().st_size
        if file_size <= offset:
            return [], offset

        items: list[dict] = []
        with open(self._signal_log_path) as f:
            f.seek(offset)
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    items.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

        return items, file_size

    def get_unanalyzed_signals_from_log(self) -> tuple[str, int]:
        """Read signals added since last analysis. Returns (signals_text, new_offset)."""
        marker_path = DEJA_HOME / "last_integration_offset"
        offset = 0
        if marker_path.exists():
            try:
                offset = int(marker_path.read_text().strip())
            except (ValueError, OSError):
                offset = 0

        if not self._signal_log_path.exists():
            return "", 0

        file_size = self._signal_log_path.stat().st_size
        if file_size <= offset:
            return "", offset  # no new data

        lines: list[str] = []
        with open(self._signal_log_path) as f:
            f.seek(offset)
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    ts = d.get("timestamp", "")
                    source = d.get("source", "?")
                    sender = d.get("sender", "?")
                    text = d.get("text", "")[:400]
                    lines.append(f"[{ts}] [{source}] {sender}: {text}")
                except json.JSONDecodeError:
                    continue

        # Cap at 200 most recent
        if len(lines) > 200:
            older_count = len(lines) - 200
            lines = [f"({older_count} older signals omitted)"] + lines[-200:]

        return "\n".join(lines), file_size

    def save_analysis_marker(self, offset: int) -> None:
        """Save the analysis byte offset."""
        marker_path = DEJA_HOME / "last_integration_offset"
        marker_path.write_text(str(offset))

    def get_recent_signals_from_log(self, minutes: int = 5) -> str:
        """Read observations.jsonl and return all signals from the last N minutes as formatted text."""
        from datetime import timedelta
        cutoff = datetime.now() - timedelta(minutes=minutes)
        lines: list[str] = []
        if not self._signal_log_path.exists():
            return ""
        try:
            with open(self._signal_log_path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                        ts = datetime.fromisoformat(d["timestamp"])
                        if ts >= cutoff:
                            hm = ts.strftime("%H:%M")
                            lines.append(f"[{hm}] [{d['source']}] {d['sender']}: {d['text']}")
                    except (json.JSONDecodeError, KeyError, ValueError):
                        continue
        except Exception:
            log.exception("Failed to read signal log for recent signals")
        return "\n".join(lines)

