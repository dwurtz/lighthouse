"""Signal-health state machine + watchdog tests.

Locks in the contract that collectors leave an auditable trail:
- consecutive errors don't flood (one audit row each, but only one
  ``collector_ok`` on recovery)
- heartbeats are throttled to once per hour per source while healthy
- ``collector_stalled`` respects the awake gate and its dedupe window
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from deja import audit
from deja import signal_health as sh


def _read_audit(home) -> list[dict]:
    path = home / "audit.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# Keep a handle on the real is_awake since one test below exercises it.
_REAL_IS_AWAKE = sh.is_awake


@pytest.fixture(autouse=True)
def _force_awake(request, monkeypatch):
    """Default to awake so tests don't need to fake a screenshot sidecar.
    Tests that need the real implementation mark themselves with
    ``@pytest.mark.real_awake`` to opt out."""
    if request.node.get_closest_marker("real_awake"):
        yield
        return
    monkeypatch.setattr(sh, "is_awake", lambda now=None: True)
    yield


# ---------------------------------------------------------------------------
# State transitions
# ---------------------------------------------------------------------------


def test_error_then_recovery_emits_one_ok_row(isolated_home):
    home, _ = isolated_home
    tracker = sh.SourceHealthTracker()
    t0 = datetime(2026, 4, 12, 12, 0, tzinfo=timezone.utc)

    tracker.record_error("email", "AuthError: token expired", now=t0)
    tracker.record_error("email", "AuthError: token expired", now=t0 + timedelta(seconds=30))
    tracker.record_error("email", "AuthError: token expired", now=t0 + timedelta(seconds=60))
    tracker.record_success("email", now=t0 + timedelta(seconds=90))

    rows = _read_audit(home)
    errs = [r for r in rows if r["action"] == "collector_error"]
    oks = [r for r in rows if r["action"] == "collector_ok"]
    assert len(errs) == 3
    assert all(r["target"] == "email" for r in errs)
    assert len(oks) == 1
    assert oks[0]["target"] == "email"
    assert "recovered after 3 errors" in oks[0]["reason"]


def test_steady_state_success_does_not_flood_heartbeats(isolated_home):
    home, _ = isolated_home
    tracker = sh.SourceHealthTracker()
    t0 = datetime(2026, 4, 12, 12, 0, tzinfo=timezone.utc)

    # First success writes one heartbeat (no prior heartbeat → due).
    tracker.record_success("imessage", now=t0)
    # Many more successes within the hour should NOT write more rows.
    for i in range(1, 30):
        tracker.record_success("imessage", now=t0 + timedelta(minutes=i))

    rows = _read_audit(home)
    oks = [r for r in rows if r["action"] == "collector_ok" and r["target"] == "imessage"]
    assert len(oks) == 1
    assert oks[0]["reason"] == "heartbeat"


def test_heartbeat_fires_once_per_hour(isolated_home):
    home, _ = isolated_home
    tracker = sh.SourceHealthTracker()
    t0 = datetime(2026, 4, 12, 12, 0, tzinfo=timezone.utc)

    tracker.record_success("imessage", now=t0)
    tracker.record_success("imessage", now=t0 + timedelta(minutes=59))
    tracker.record_success("imessage", now=t0 + timedelta(minutes=61))
    tracker.record_success("imessage", now=t0 + timedelta(minutes=121))

    rows = _read_audit(home)
    oks = [r for r in rows if r["action"] == "collector_ok" and r["reason"] == "heartbeat"]
    assert len(oks) == 3


def test_heartbeat_suppressed_while_asleep(isolated_home, monkeypatch):
    home, _ = isolated_home
    monkeypatch.setattr(sh, "is_awake", lambda now=None: False)

    tracker = sh.SourceHealthTracker()
    t0 = datetime(2026, 4, 12, 2, 0, tzinfo=timezone.utc)
    tracker.record_success("imessage", now=t0)
    tracker.record_success("imessage", now=t0 + timedelta(hours=2))

    rows = _read_audit(home)
    assert not any(
        r["action"] == "collector_ok" and r["target"] == "imessage" for r in rows
    )


def test_recovery_row_emitted_even_while_asleep(isolated_home, monkeypatch):
    """Recovery events are important enough to bypass the sleep gate."""
    home, _ = isolated_home
    monkeypatch.setattr(sh, "is_awake", lambda now=None: False)

    tracker = sh.SourceHealthTracker()
    t0 = datetime(2026, 4, 12, 3, 0, tzinfo=timezone.utc)
    tracker.record_error("email", "boom", now=t0)
    tracker.record_success("email", now=t0 + timedelta(seconds=60))

    rows = _read_audit(home)
    oks = [r for r in rows if r["action"] == "collector_ok" and r["target"] == "email"]
    assert len(oks) == 1
    assert "recovered" in oks[0]["reason"]


# ---------------------------------------------------------------------------
# Watchdog / stall detection
# ---------------------------------------------------------------------------


def test_watchdog_flags_stall_past_threshold(isolated_home):
    home, _ = isolated_home
    tracker = sh.SourceHealthTracker()
    t0 = datetime(2026, 4, 12, 12, 0, tzinfo=timezone.utc)

    # Email threshold is 30 min. Mark last success 45 min ago.
    tracker.last_ok_at["email"] = t0 - timedelta(minutes=45)

    flagged = sh.run_watchdog_once(tracker, now=t0)
    assert "email" in flagged

    rows = _read_audit(home)
    stalled = [r for r in rows if r["action"] == "collector_stalled"]
    assert len(stalled) == 1
    assert stalled[0]["target"] == "email"


def test_watchdog_skipped_when_asleep(isolated_home, monkeypatch):
    home, _ = isolated_home
    monkeypatch.setattr(sh, "is_awake", lambda now=None: False)

    tracker = sh.SourceHealthTracker()
    t0 = datetime(2026, 4, 12, 3, 0, tzinfo=timezone.utc)
    tracker.last_ok_at["email"] = t0 - timedelta(hours=5)

    flagged = sh.run_watchdog_once(tracker, now=t0)
    assert flagged == []
    assert _read_audit(home) == []


def test_watchdog_dedupes_within_hour(isolated_home):
    home, _ = isolated_home
    tracker = sh.SourceHealthTracker()
    t0 = datetime(2026, 4, 12, 12, 0, tzinfo=timezone.utc)
    tracker.last_ok_at["email"] = t0 - timedelta(hours=2)

    sh.run_watchdog_once(tracker, now=t0)
    sh.run_watchdog_once(tracker, now=t0 + timedelta(minutes=1))
    sh.run_watchdog_once(tracker, now=t0 + timedelta(minutes=30))

    stalled = [r for r in _read_audit(home) if r["action"] == "collector_stalled"]
    assert len(stalled) == 1

    # After the re-emit window, it should fire again.
    sh.run_watchdog_once(tracker, now=t0 + timedelta(minutes=61))
    stalled = [r for r in _read_audit(home) if r["action"] == "collector_stalled"]
    assert len(stalled) == 2


def test_watchdog_skips_source_with_no_history(isolated_home):
    """A source that has never produced anything shouldn't flap at startup."""
    home, _ = isolated_home
    tracker = sh.SourceHealthTracker()
    t0 = datetime(2026, 4, 12, 12, 0, tzinfo=timezone.utc)

    flagged = sh.run_watchdog_once(tracker, now=t0)
    assert flagged == []
    assert _read_audit(home) == []


def test_watchdog_prefers_newest_of_tracker_or_observations(isolated_home):
    home, _ = isolated_home
    t0 = datetime(2026, 4, 12, 12, 0, tzinfo=timezone.utc)

    # tracker says last_ok was 2h ago (would stall), observations
    # file has a fresh email signal — watchdog should not flag.
    obs_path = home / "observations.jsonl"
    fresh_ts = (t0 - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    obs_path.write_text(json.dumps({
        "source": "email", "sender": "x", "text": "y",
        "timestamp": fresh_ts, "id_key": "k1",
    }) + "\n")

    tracker = sh.SourceHealthTracker()
    tracker.last_ok_at["email"] = t0 - timedelta(hours=2)

    flagged = sh.run_watchdog_once(tracker, now=t0, observations_log=obs_path)
    assert "email" not in flagged


# ---------------------------------------------------------------------------
# Awake detection
# ---------------------------------------------------------------------------


@pytest.mark.real_awake
def test_is_awake_reads_screenshot_sidecar(isolated_home, monkeypatch):
    home, _ = isolated_home
    sidecar = home / "latest_screen_ts.txt"
    monkeypatch.setattr(sh._config, "DEJA_HOME", home)

    # Fresh — awake.
    sidecar.write_text(f"{datetime.now(timezone.utc).timestamp()}")
    sh._reset_awake_cache()
    assert sh.is_awake() is True

    # Stale — asleep.
    stale = datetime.now(timezone.utc).timestamp() - 30 * 60
    sidecar.write_text(f"{stale}")
    sh._reset_awake_cache()
    assert sh.is_awake() is False


@pytest.mark.real_awake
def test_is_awake_missing_sidecar_defaults_awake(isolated_home, monkeypatch):
    home, _ = isolated_home
    monkeypatch.setattr(sh._config, "DEJA_HOME", home)
    sh._reset_awake_cache()
    assert sh.is_awake() is True
