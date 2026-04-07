"""Observer -- orchestrates all signal sources with deduplication."""

from __future__ import annotations

import logging
import json
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

from lighthouse.config import IGNORED_APPS, LIGHTHOUSE_HOME
from lighthouse.observations.active_app import get_active_app
from lighthouse.observations.browser import collect_browser_history
from lighthouse.observations.calendar import collect_upcoming_events, collect_past_events
from lighthouse.observations.clipboard import collect_clipboard
from lighthouse.observations.drive import collect_recent_drive_activity
from lighthouse.observations.email import collect_recent_emails
from lighthouse.observations.imessage import collect_imessages
from lighthouse.observations.screenshot import capture_screenshot_if_changed
from lighthouse.observations.tasks import collect_pending_tasks
from lighthouse.observations.types import Observation
from lighthouse.observations.whatsapp import collect_whatsapp
from lighthouse.observations.meet import collect_recent_transcripts

log = logging.getLogger(__name__)


class Observer:
    """Collects signals from all sources, deduplicating across calls."""

    def __init__(self, history_size: int = 10000) -> None:
        self._seen_ids: set[str] = set()
        self._last_app: str = ""
        self._last_title: str = ""
        self._screenshot_counter: int = 0
        self._screenshot_every: int = 1
        self._email_counter: int = 0
        self._calendar_counter: int = 0
        self._drive_counter: int = 0
        self._tasks_counter: int = 0
        self._browser_counter: int = 0
        self._gws_every: int = 5
        self._browser_every: int = 3  # ~9s at OBSERVE_INTERVAL=3
        self.recent_history: deque[Observation] = deque(maxlen=history_size)
        self._signal_log_path = LIGHTHOUSE_HOME / "observations.jsonl"
        self._load_history()

    def collect_all(self) -> list[Observation]:
        """
        Run all collectors, deduplicate against previously seen ids,
        return only new signals.
        """
        raw: list[Observation] = []

        # Messages
        try:
            raw.extend(collect_imessages())
        except Exception:
            log.exception("iMessage collector error")

        try:
            raw.extend(collect_whatsapp())
        except Exception:
            log.exception("WhatsApp collector error")

        # Clipboard
        try:
            clip = collect_clipboard()
            if clip:
                raw.append(clip)
        except Exception:
            log.exception("Clipboard collector error")

        # Active app / window — NOT emitted as a signal. Captured solely to
        # decide whether the visual state changed enough to warrant a new
        # screenshot. The screenshot is the signal; the app/title change is
        # just the trigger. Chrome tabs are covered the same way.
        current_app = ""
        current_title = ""
        try:
            current_app, current_title = get_active_app()
        except Exception:
            log.exception("Active app probe error")

        # Browser history — every 3rd cycle (~9s). Reads only the active
        # profile of each installed Chromium browser (Chrome, Arc, etc.),
        # chosen from each browser's Local State `profile.last_used` field.
        self._browser_counter += 1
        if self._browser_counter >= self._browser_every:
            self._browser_counter = 0
            try:
                raw.extend(collect_browser_history(since_minutes=10))
            except Exception:
                log.exception("Browser history collector error")

        # Email — every 5th cycle
        self._email_counter += 1
        if self._email_counter >= self._gws_every:
            self._email_counter = 0
            try:
                raw.extend(collect_recent_emails())
            except Exception:
                log.exception("Email collector error")

        # Calendar — every 5th cycle (both upcoming + recently finished)
        self._calendar_counter += 1
        if self._calendar_counter >= self._gws_every:
            self._calendar_counter = 0
            try:
                raw.extend(collect_upcoming_events())
            except Exception:
                log.exception("Calendar upcoming collector error")
            try:
                raw.extend(collect_past_events())
            except Exception:
                log.exception("Calendar past collector error")

        # Drive — every 5th cycle
        self._drive_counter += 1
        if self._drive_counter >= self._gws_every:
            self._drive_counter = 0
            try:
                raw.extend(collect_recent_drive_activity())
            except Exception:
                log.exception("Drive collector error")

        # Tasks — every 5th cycle
        self._tasks_counter += 1
        if self._tasks_counter >= self._gws_every:
            self._tasks_counter = 0
            try:
                raw.extend(collect_pending_tasks())
            except Exception:
                log.exception("Tasks collector error")

        # Meet transcripts — every 5th cycle (Drive API query)
        if self._email_counter == 0:  # piggyback on email counter
            try:
                raw.extend(collect_recent_transcripts())
            except Exception:
                log.exception("Meet transcript collector error")

        # Microphone: handled entirely by the web server via
        # /api/mic/start and /api/mic/stop. Nothing to poll here — mic
        # transcripts are written to observations.jsonl directly when the
        # user ends a session.

        # Screenshot — on app/window change OR every 5 seconds minimum.
        # Gated by config.SCREENSHOT_ENABLED so the user can disable the
        # collector if macOS Screen Recording permission is misbehaving
        # (the only observation source that needs TCC screen-recording).
        from lighthouse.config import SCREENSHOT_ENABLED
        if SCREENSHOT_ENABLED:
            try:
                self._screenshot_counter += 1
                context_changed = self.should_screenshot(current_app, current_title)
                periodic = self._screenshot_counter >= 2  # 2 cycles × 3s = 6s

                if context_changed or periodic:
                    self._screenshot_counter = 0
                    screen = capture_screenshot_if_changed()
                    if screen:
                        raw.append(screen)
            except Exception:
                log.exception("Screenshot collector error")

        # Resolve contact names for message signals
        try:
            from lighthouse.observations.contacts import resolve_contact
            for sig in raw:
                if sig.source in ("imessage", "whatsapp") and sig.sender != "You":
                    resolved = resolve_contact(sig.sender)
                    if resolved:
                        sig.sender = f"{resolved} ({sig.sender})" if "+" in sig.sender else resolved
        except Exception:
            pass

        # Thread conversations — group consecutive messages into threads
        try:
            from lighthouse.observations.threads import thread_signals
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
        recent = []   # last hour
        older = []    # older than 1 hour

        for s in unmatched:
            age = (now - s.timestamp).total_seconds()
            if age < 3600:
                recent.append(s)
            else:
                older.append(s)

        lines = []

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
            LIGHTHOUSE_HOME.mkdir(parents=True, exist_ok=True)
            with open(self._signal_log_path, "a") as f:
                f.write(json.dumps({
                    "source": sig.source,
                    "sender": sig.sender,
                    "text": sig.text[:500],
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
        marker_path = LIGHTHOUSE_HOME / "last_integration_offset"
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
        marker_path = LIGHTHOUSE_HOME / "last_integration_offset"
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

        lines = []
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
        marker_path = LIGHTHOUSE_HOME / "last_integration_offset"
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

    def should_screenshot(self, app: str, title: str) -> bool:
        """Return True if app/title changed since last check."""
        if app in IGNORED_APPS:
            changed = False
        else:
            changed = app != self._last_app or title != self._last_title
        self._last_app = app
        self._last_title = title
        return changed
