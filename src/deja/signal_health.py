"""Collector-level signal health — per-source audit trail + watchdog.

Every signal source (email, imessage, whatsapp, screenshot, browser, drive,
clipboard, calendar, tasks, typed) needs an auditable trail of when it
was working and when it wasn't. Without one, a dead iMessage socket or a
gws auth lapse just quietly produces no signals, and the user only notices
days later when the wiki stops updating about texts.

This module owns three responsibilities:

1. ``SourceHealthTracker`` — in-memory per-source state machine wired into
   ``Observer.collect_all()``. Records ``collector_error`` on every raise,
   ``collector_ok`` as a recovery marker on error→ok transitions, and a
   ``collector_ok`` heartbeat once per hour while a source is healthy.

2. ``run_watchdog_once()`` — scheduled every 60s by the agent loop. Reads
   the last successful ingest time per source (from the in-memory tracker
   plus a scan of observations.jsonl tail) and emits ``collector_stalled``
   when a source has been silent past its expected interval, gated on
   ``is_awake()`` so we don't flag stalls while the Mac was asleep.

3. ``is_awake()`` — best-effort sleep detector. Uses the Swift-side
   ``latest_screen_ts.txt`` sidecar as a liveness proxy (updated on every
   screenshot capture while the agent is running); falls back to awake so
   a missing sidecar never mis-labels a real stall as sleep.

All writes funnel through ``deja.audit.record`` — there is no second
logger and no separate ``signal_health.jsonl``.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from deja import audit
from deja import config as _config

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

# Expected interval per source (minutes). "Stalled" means silent longer
# than this during waking hours. Tuned for each source's realistic cadence:
# screenshot captures every few seconds, email uses gws delta (~30m),
# message sources depend on the user actually chatting, drive/calendar/
# tasks are low volume even when active.
EXPECTED_INTERVAL_MINUTES: dict[str, int] = {
    "screenshot": 2,
    "email": 30,
    "imessage": 60,
    "whatsapp": 60,
    "clipboard": 60,
    "browser": 30,
    "drive": 120,
    "calendar": 120,
    "tasks": 120,
    "typed": 120,
}

# Heartbeat cadence — emit one collector_ok audit row per source per this
# many minutes while the source is continuously healthy. Picked to match
# the coarsest expected interval so heartbeats never outrun real signals.
HEARTBEAT_INTERVAL_MINUTES = 60

# Re-emit a stall notice at most once per hour per source while still
# stalled — otherwise a dead source would flood audit.jsonl with one row
# every 60s forever.
STALL_REEMIT_INTERVAL_MINUTES = 60

# If the screenshot capture sidecar is older than this, assume the Mac
# may be asleep / the agent isn't running and skip stall detection.
AWAKE_WINDOW_MINUTES = 15


# Map from BaseObserver.name (human-readable, e.g. "iMessage") to the
# lowercase source id used in Observation.source and the UI. Observer
# classes that correspond to bundles of sources (e.g. MeetObserver writes
# source="meet_transcript") are not tracked here — only first-class
# sources the user recognizes.
OBSERVER_NAME_TO_SOURCE: dict[str, str] = {
    "iMessage": "imessage",
    "WhatsApp": "whatsapp",
    "Clipboard": "clipboard",
    "TypedContent": "typed",
    "Email": "email",
    "Calendar": "calendar",
    "Drive": "drive",
    "Tasks": "tasks",
    "Browser": "browser",
    "Screenshot": "screenshot",
}


def source_id_for(observer_name: str) -> Optional[str]:
    """Return the canonical source id for a BaseObserver.name, or None
    if the observer isn't a first-class tracked source."""
    return OBSERVER_NAME_TO_SOURCE.get(observer_name)


# ---------------------------------------------------------------------------
# Sleep detection
# ---------------------------------------------------------------------------

_awake_cache: dict = {"value": True, "at": 0.0}
_AWAKE_CACHE_TTL_SECONDS = 60


def is_awake(now: Optional[datetime] = None) -> bool:
    """Return True if the Mac appears to be awake + agent running.

    Uses the Swift-side ``~/.deja/latest_screen_ts.txt`` sidecar, which
    is rewritten on every screenshot capture (~every few seconds while
    the agent is running). If it was updated within ``AWAKE_WINDOW_MINUTES``
    we're confident the machine is up and the collectors should be firing.

    Cached for 60s so calling this for every source on every watchdog
    tick costs one ``stat`` call per minute.

    Fails safe: any error or missing file → returns ``True``. The cost of
    a false "awake" is at most one spurious ``collector_stalled`` row;
    the cost of a false "asleep" is silently hiding a real dead source.
    """
    now = now or datetime.now(timezone.utc)
    now_ts = now.timestamp()
    if now_ts - _awake_cache["at"] < _AWAKE_CACHE_TTL_SECONDS:
        return bool(_awake_cache["value"])

    awake = True  # default: fail-safe
    try:
        sidecar = _config.DEJA_HOME / "latest_screen_ts.txt"
        if sidecar.exists():
            raw = sidecar.read_text(errors="replace").strip()
            try:
                sidecar_ts = float(raw)
                age = now_ts - sidecar_ts
                awake = age < AWAKE_WINDOW_MINUTES * 60
            except ValueError:
                awake = True
    except Exception:
        log.debug("is_awake sidecar read failed", exc_info=True)
        awake = True

    _awake_cache["value"] = awake
    _awake_cache["at"] = now_ts
    return awake


def _reset_awake_cache() -> None:
    """Test helper — drop the awake cache so the next call re-reads."""
    _awake_cache["at"] = 0.0


# ---------------------------------------------------------------------------
# Per-source tracker — wired into Observer.collect_all()
# ---------------------------------------------------------------------------


@dataclass
class SourceHealthTracker:
    """Per-source state machine kept on the Observer instance.

    Fields are dicts keyed by source id (e.g. ``"email"``). All times are
    tz-aware UTC datetimes.
    """

    last_state: dict[str, str] = field(default_factory=dict)  # "ok" | "error"
    last_ok_at: dict[str, datetime] = field(default_factory=dict)
    last_error_at: dict[str, datetime] = field(default_factory=dict)
    last_error_reason: dict[str, str] = field(default_factory=dict)
    last_heartbeat_at: dict[str, datetime] = field(default_factory=dict)
    error_count_since_ok: dict[str, int] = field(default_factory=dict)
    last_stall_audit_at: dict[str, datetime] = field(default_factory=dict)

    def record_success(
        self,
        source: str,
        now: Optional[datetime] = None,
    ) -> None:
        """Record one successful collect() call for ``source``.

        Emits a recovery ``collector_ok`` audit row on an error→ok
        transition, and an hourly heartbeat while continuously healthy
        (suppressed during sleep).
        """
        if not source:
            return
        now = now or datetime.now(timezone.utc)
        prev_state = self.last_state.get(source)
        n_errors = self.error_count_since_ok.get(source, 0)
        self.last_state[source] = "ok"
        self.last_ok_at[source] = now

        if prev_state == "error":
            # Recovery event — always emit regardless of sleep. Recoveries
            # are rare and important enough to never be suppressed.
            audit.record(
                "collector_ok",
                target=source,
                reason=f"recovered after {n_errors} error{'s' if n_errors != 1 else ''}",
                trigger={"kind": "manual", "detail": "collector_recovery"},
            )
            self.error_count_since_ok[source] = 0
            self.last_heartbeat_at[source] = now
            return

        self.error_count_since_ok[source] = 0

        # Hourly heartbeat. Suppress during sleep so we don't stamp
        # fake "ok" rows over quiet sleep windows.
        last_hb = self.last_heartbeat_at.get(source)
        due = last_hb is None or (now - last_hb) >= timedelta(
            minutes=HEARTBEAT_INTERVAL_MINUTES
        )
        if due and is_awake(now):
            audit.record(
                "collector_ok",
                target=source,
                reason="heartbeat",
                trigger={"kind": "manual", "detail": "collector_heartbeat"},
            )
            self.last_heartbeat_at[source] = now

    def record_error(
        self,
        source: str,
        reason: str,
        now: Optional[datetime] = None,
    ) -> None:
        """Record one failed collect() call for ``source``."""
        if not source:
            return
        now = now or datetime.now(timezone.utc)
        self.last_state[source] = "error"
        self.last_error_at[source] = now
        self.last_error_reason[source] = reason[:200]
        self.error_count_since_ok[source] = (
            self.error_count_since_ok.get(source, 0) + 1
        )
        audit.record(
            "collector_error",
            target=source,
            reason=reason[:400],
            trigger={"kind": "manual", "detail": "collector_error"},
        )


# ---------------------------------------------------------------------------
# Watchdog
# ---------------------------------------------------------------------------


def _read_last_observation_times(
    observations_log: Optional[Path] = None,
    max_lines: int = 5000,
) -> dict[str, datetime]:
    """Scan the tail of observations.jsonl for the newest row per source.

    Returns ``{source_id: timestamp}``. Rows without parseable timestamps
    or source fields are skipped. ``meet_transcript`` and other non-
    tracked sources are returned as-is but filtered by the caller.
    """
    path = observations_log or (_config.DEJA_HOME / "observations.jsonl")
    latest: dict[str, datetime] = {}
    if not path.exists():
        return latest
    try:
        # Cheap tail: read the last ~1MB. observations.jsonl rows are
        # typically <500 bytes, so that's thousands of recent rows.
        with open(path, "rb") as f:
            try:
                f.seek(-1_000_000, 2)
            except OSError:
                f.seek(0)
            tail = f.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return latest

    from deja.observations.time_utils import parse_observation_ts

    for line in tail[-max_lines:]:
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
            src = d.get("source")
            ts_str = d.get("timestamp")
            if not src or not ts_str:
                continue
            # Observation timestamps are naive local (collector convention).
            # parse_observation_ts treats naive as local and returns aware
            # UTC — required for comparison with datetime.now(timezone.utc)
            # in run_watchdog_once / compute_signal_health. Using
            # ``.replace(tzinfo=utc)`` here silently shifted ages by
            # UTC_offset hours on non-UTC machines and made the watchdog
            # flag every collector as "stalled" on every tick.
            ts = parse_observation_ts(ts_str)
            cur = latest.get(src)
            if cur is None or ts > cur:
                latest[src] = ts
        except (json.JSONDecodeError, ValueError, KeyError):
            continue
    return latest


def run_watchdog_once(
    tracker: SourceHealthTracker,
    now: Optional[datetime] = None,
    observations_log: Optional[Path] = None,
) -> list[str]:
    """Check every tracked source; emit ``collector_stalled`` if overdue.

    Stall logic per source: take the most recent of (last successful
    collect in ``tracker``) and (newest row in observations.jsonl). If
    the gap to ``now`` exceeds the expected interval AND the Mac is
    awake AND we haven't already logged a stall for this source in the
    last ``STALL_REEMIT_INTERVAL_MINUTES``, write one audit row.

    Returns the list of source ids that were flagged (for logging/tests).
    """
    now = now or datetime.now(timezone.utc)
    if not is_awake(now):
        return []

    obs_latest = _read_last_observation_times(observations_log)
    flagged: list[str] = []

    for source, interval_min in EXPECTED_INTERVAL_MINUTES.items():
        last_signal = obs_latest.get(source)
        last_ok = tracker.last_ok_at.get(source)
        # Take the most recent signal of life; if none, the source has
        # never produced anything in this run or on disk — we can't tell
        # the difference between "just installed" and "broken for a week"
        # from this signal alone, so skip (the startup boundary handles it).
        reference: Optional[datetime] = None
        for candidate in (last_signal, last_ok):
            if candidate is None:
                continue
            if reference is None or candidate > reference:
                reference = candidate
        if reference is None:
            continue

        gap = now - reference
        if gap < timedelta(minutes=interval_min):
            continue

        # Dedupe: don't re-emit the same stall every 60s.
        last_stall = tracker.last_stall_audit_at.get(source)
        if last_stall is not None and (now - last_stall) < timedelta(
            minutes=STALL_REEMIT_INTERVAL_MINUTES
        ):
            continue

        gap_min = int(gap.total_seconds() // 60)
        audit.record(
            "collector_stalled",
            target=source,
            reason=(
                f"no signal for {gap_min}m "
                f"(expected every {interval_min}m)"
            ),
            trigger={"kind": "manual", "detail": "collector_watchdog"},
        )
        tracker.last_stall_audit_at[source] = now
        flagged.append(source)

    return flagged


# ---------------------------------------------------------------------------
# /api/signal_health endpoint computation
# ---------------------------------------------------------------------------


def compute_signal_health(
    now: Optional[datetime] = None,
    audit_log: Optional[Path] = None,
    observations_log: Optional[Path] = None,
) -> dict:
    """Build the payload for ``GET /api/signal_health``.

    Walks the last day of ``~/.deja/audit.jsonl`` for ``collector_*`` rows
    to recover per-source last-ok/last-error state across restarts (the
    in-memory tracker is ephemeral), cross-references the observations
    log tail for last-signal times, and applies ``EXPECTED_INTERVAL_MINUTES``
    to derive ``status``.
    """
    now = now or datetime.now(timezone.utc)
    audit_path = audit_log or audit.AUDIT_LOG
    cutoff = now - timedelta(days=1)

    per_source: dict[str, dict] = {
        src: {
            "last_ok_at": None,
            "last_error_at": None,
            "last_error_reason": None,
            "last_stalled_at": None,
        }
        for src in EXPECTED_INTERVAL_MINUTES
    }

    if audit_path.exists():
        try:
            with open(audit_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    action = d.get("action", "")
                    if not action.startswith("collector_"):
                        continue
                    target = d.get("target", "")
                    if target not in per_source:
                        continue
                    ts_str = (d.get("ts") or "").replace("Z", "+00:00")
                    try:
                        ts = datetime.fromisoformat(ts_str)
                    except ValueError:
                        continue
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    if ts < cutoff:
                        continue
                    row = per_source[target]
                    if action == "collector_ok":
                        cur = row["last_ok_at"]
                        if cur is None or ts > cur:
                            row["last_ok_at"] = ts
                    elif action == "collector_error":
                        cur = row["last_error_at"]
                        if cur is None or ts > cur:
                            row["last_error_at"] = ts
                            row["last_error_reason"] = d.get("reason", "")
                    elif action == "collector_stalled":
                        cur = row["last_stalled_at"]
                        if cur is None or ts > cur:
                            row["last_stalled_at"] = ts
        except OSError:
            log.debug("compute_signal_health: audit read failed", exc_info=True)

    obs_latest = _read_last_observation_times(observations_log)
    awake = is_awake(now)
    sources_out: list[dict] = []
    for src, interval_min in EXPECTED_INTERVAL_MINUTES.items():
        row = per_source[src]
        obs_ts = obs_latest.get(src)
        candidates = [t for t in (row["last_ok_at"], obs_ts) if t is not None]
        last_signal_at = max(candidates) if candidates else None

        minutes_since = None
        if last_signal_at is not None:
            minutes_since = int((now - last_signal_at).total_seconds() // 60)

        # Status ladder:
        #   error   if the newest collector_* row is collector_error
        #   stalled if we're past threshold and awake
        #   ok      otherwise
        status = "ok"
        newest_ok = row["last_ok_at"]
        newest_err = row["last_error_at"]
        if newest_err is not None and (
            newest_ok is None or newest_err > newest_ok
        ):
            status = "error"
        elif (
            minutes_since is not None
            and minutes_since > interval_min
            and awake
        ):
            status = "stalled"
        elif last_signal_at is None and awake:
            # Never seen a signal — treat as stalled only if expected
            # source, conservatively mark unknown (displayed as "stalled"
            # so the UI shows it needs attention).
            status = "stalled"

        sources_out.append(
            {
                "id": src,
                "status": status,
                "last_signal_at": _iso(last_signal_at),
                "last_ok_at": _iso(newest_ok),
                "last_error_at": _iso(newest_err),
                "last_error_reason": row["last_error_reason"],
                "expected_interval_minutes": interval_min,
                "minutes_since_last_signal": minutes_since,
            }
        )

    return {
        "generated_at": _iso(now),
        "awake": awake,
        "sources": sources_out,
    }


def _iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )
