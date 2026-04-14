"""Shape + behavior tests for GET /api/signal_health.

The Swift tray UI consumes this endpoint. The contract is:
- Every source in ``EXPECTED_INTERVAL_MINUTES`` appears in ``sources``.
- ``status`` is one of ``ok | stalled | error``.
- ``minutes_since_last_signal`` is null if we've never seen the source.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from deja import audit
from deja import signal_health as sh
from deja.web.status_routes import router


@pytest.fixture
def client(isolated_home, monkeypatch):
    # Force awake so we get deterministic stall behavior.
    monkeypatch.setattr(sh, "is_awake", lambda now=None: True)
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _write_audit(home, rows):
    path = home / "audit.jsonl"
    with open(path, "a") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def test_shape_and_all_sources_present(client, isolated_home):
    resp = client.get("/api/signal_health")
    assert resp.status_code == 200
    data = resp.json()

    assert "generated_at" in data
    assert "awake" in data
    assert data["awake"] is True

    ids = {s["id"] for s in data["sources"]}
    assert ids == set(sh.EXPECTED_INTERVAL_MINUTES.keys())

    for s in data["sources"]:
        assert s["status"] in {"ok", "stalled", "error"}
        assert "last_signal_at" in s
        assert "last_ok_at" in s
        assert "last_error_at" in s
        assert "last_error_reason" in s
        assert "expected_interval_minutes" in s
        assert "minutes_since_last_signal" in s


def test_reports_ok_when_recent_audit_ok(client, isolated_home):
    home, _ = isolated_home
    now = datetime.now(timezone.utc)
    _write_audit(home, [
        {
            "ts": _iso(now - timedelta(minutes=5)),
            "cycle": "", "trigger": {"kind": "manual", "detail": "collector_heartbeat"},
            "action": "collector_ok", "target": "email", "reason": "heartbeat",
        },
    ])
    data = client.get("/api/signal_health").json()
    email = next(s for s in data["sources"] if s["id"] == "email")
    assert email["status"] == "ok"
    assert email["minutes_since_last_signal"] == 5


def test_reports_error_when_last_row_is_error(client, isolated_home):
    home, _ = isolated_home
    now = datetime.now(timezone.utc)
    _write_audit(home, [
        {
            "ts": _iso(now - timedelta(minutes=10)),
            "cycle": "", "trigger": {"kind": "manual", "detail": "collector_heartbeat"},
            "action": "collector_ok", "target": "email", "reason": "heartbeat",
        },
        {
            "ts": _iso(now - timedelta(minutes=2)),
            "cycle": "", "trigger": {"kind": "manual", "detail": "collector_error"},
            "action": "collector_error", "target": "email", "reason": "HTTP 500",
        },
    ])
    data = client.get("/api/signal_health").json()
    email = next(s for s in data["sources"] if s["id"] == "email")
    assert email["status"] == "error"
    assert email["last_error_reason"] == "HTTP 500"


def test_reports_stalled_when_past_threshold(client, isolated_home):
    home, _ = isolated_home
    now = datetime.now(timezone.utc)
    # Email threshold is 30m; last ok was 90m ago.
    _write_audit(home, [
        {
            "ts": _iso(now - timedelta(minutes=90)),
            "cycle": "", "trigger": {"kind": "manual", "detail": "collector_heartbeat"},
            "action": "collector_ok", "target": "email", "reason": "heartbeat",
        },
    ])
    data = client.get("/api/signal_health").json()
    email = next(s for s in data["sources"] if s["id"] == "email")
    assert email["status"] == "stalled"
    assert email["minutes_since_last_signal"] >= 90


def test_observations_log_counts_as_signal(client, isolated_home):
    home, _ = isolated_home
    now = datetime.now(timezone.utc)
    obs_path = home / "observations.jsonl"
    obs_path.write_text(json.dumps({
        "source": "email", "sender": "a", "text": "b",
        "timestamp": _iso(now - timedelta(minutes=3)),
        "id_key": "k1",
    }) + "\n")

    data = client.get("/api/signal_health").json()
    email = next(s for s in data["sources"] if s["id"] == "email")
    assert email["status"] == "ok"
    assert email["minutes_since_last_signal"] == 3
