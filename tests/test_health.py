"""Tests for the live health monitor (``observability/health.py``).

Uses the ``isolated_home`` fixture from conftest.py so every test runs
against a fresh tmp ``~/.deja`` + ``~/Deja``. Network calls are
monkeypatched — nothing here hits the real proxy.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

from deja.observability.health import (
    HealthChecker,
    _aggregate_overall,
    _atomic_write_json,
    _last_error_request_id,
    _truncate,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _now_iso_offset(seconds_ago: float) -> str:
    dt = datetime.now(timezone.utc).timestamp() - seconds_ago
    return (
        datetime.fromtimestamp(dt, tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _write_screen_ts(home: Path, seconds_ago: float) -> None:
    ts = time.time() - seconds_ago
    (home / "latest_screen_ts.txt").write_text(f"{ts}\n")


def _append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


class _StubTransport(httpx.AsyncBaseTransport):
    def __init__(self, status: int = 200, exc: Exception | None = None):
        self.status = status
        self.exc = exc

    async def handle_async_request(self, request):  # type: ignore[override]
        if self.exc:
            raise self.exc
        return httpx.Response(self.status, content=b"{}", request=request)


def _patch_httpx(monkeypatch, *, status: int = 200, exc: Exception | None = None):
    """Patch ``httpx.AsyncClient`` with a stubbed transport."""
    real_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = _StubTransport(status=status, exc=exc)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)


# ---------------------------------------------------------------------------
# primitives
# ---------------------------------------------------------------------------


def test_truncate_short_passes_through():
    assert _truncate("hello") == "hello"


def test_truncate_long_adds_ellipsis():
    s = "x" * 200
    out = _truncate(s)
    assert len(out) <= 120
    assert out.endswith("\u2026")


def test_aggregate_any_broken_is_broken():
    checks = [
        {"status": "ok"}, {"status": "degraded"}, {"status": "broken"},
    ]
    assert _aggregate_overall(checks) == "broken"


def test_aggregate_any_degraded_is_degraded():
    checks = [{"status": "ok"}, {"status": "degraded"}, {"status": "ok"}]
    assert _aggregate_overall(checks) == "degraded"


def test_aggregate_all_ok_is_ok():
    checks = [{"status": "ok"}, {"status": "ok"}]
    assert _aggregate_overall(checks) == "ok"


def test_aggregate_empty_is_ok():
    assert _aggregate_overall([]) == "ok"


def test_atomic_write_leaves_no_partials(isolated_home, tmp_path):
    home, _wiki = isolated_home
    path = home / "health.json"
    _atomic_write_json(path, {"x": 1})
    assert path.exists()
    # No stray tmp files left over.
    leftovers = [p for p in home.iterdir() if p.name.startswith(".health.")]
    assert leftovers == []
    assert json.loads(path.read_text()) == {"x": 1}


# ---------------------------------------------------------------------------
# last_error_request_id
# ---------------------------------------------------------------------------


def test_last_error_request_id_missing(isolated_home):
    assert _last_error_request_id() is None


def test_last_error_request_id_from_tail(isolated_home):
    home, _ = isolated_home
    errors = home / "errors.jsonl"
    _append_jsonl(errors, {"request_id": "req_aaa", "code": "x"})
    _append_jsonl(errors, {"request_id": "req_bbb", "code": "y"})
    _append_jsonl(errors, {"request_id": "req_ccc", "code": "z"})
    assert _last_error_request_id() == "req_ccc"


# ---------------------------------------------------------------------------
# individual checks
# ---------------------------------------------------------------------------


def test_check_proxy_ok(isolated_home, monkeypatch):
    _patch_httpx(monkeypatch, status=200)
    c = HealthChecker()
    out = asyncio.run(c.check_proxy())
    assert out["id"] == "proxy"
    assert out["status"] == "ok"
    assert out["fix"] is None


def test_check_proxy_broken_on_5xx(isolated_home, monkeypatch):
    _patch_httpx(monkeypatch, status=503)
    c = HealthChecker()
    out = asyncio.run(c.check_proxy())
    assert out["status"] == "broken"
    assert "503" in out["detail"]
    assert out["fix"]


def test_check_proxy_broken_on_timeout(isolated_home, monkeypatch):
    _patch_httpx(monkeypatch, exc=httpx.ConnectTimeout("slow"))
    c = HealthChecker()
    out = asyncio.run(c.check_proxy())
    assert out["status"] == "broken"
    assert "Unreachable" in out["detail"]


def test_check_recent_signals_ok(isolated_home):
    home, _ = isolated_home
    obs = home / "observations.jsonl"
    _append_jsonl(obs, {"ts": _now_iso_offset(10), "kind": "x"})
    _append_jsonl(obs, {"ts": _now_iso_offset(20), "kind": "y"})
    c = HealthChecker()
    out = asyncio.run(c.check_recent_signals())
    assert out["status"] == "ok"


def test_check_recent_signals_degraded_when_screen_live(isolated_home):
    home, _ = isolated_home
    _write_screen_ts(home, 5)  # recent
    c = HealthChecker()
    out = asyncio.run(c.check_recent_signals())
    assert out["status"] == "degraded"


def test_check_recent_signals_broken_when_dead(isolated_home):
    # No signals, no latest_screen_ts
    c = HealthChecker()
    out = asyncio.run(c.check_recent_signals())
    assert out["status"] == "broken"
    assert out["fix"]


def test_check_latest_screen_ok(isolated_home):
    home, _ = isolated_home
    _write_screen_ts(home, 5)
    c = HealthChecker()
    out = asyncio.run(c.check_latest_screen())
    assert out["status"] == "ok"


def test_check_latest_screen_degraded(isolated_home):
    home, _ = isolated_home
    _write_screen_ts(home, 60)
    c = HealthChecker()
    out = asyncio.run(c.check_latest_screen())
    assert out["status"] == "degraded"


def test_check_latest_screen_broken_old(isolated_home):
    home, _ = isolated_home
    _write_screen_ts(home, 600)
    c = HealthChecker()
    out = asyncio.run(c.check_latest_screen())
    assert out["status"] == "broken"


def test_check_latest_screen_broken_missing(isolated_home):
    c = HealthChecker()
    out = asyncio.run(c.check_latest_screen())
    assert out["status"] == "broken"
    assert "missing" in out["detail"]


def test_check_wiki_ok(isolated_home):
    _, wiki = isolated_home
    (wiki / "index.md").write_text("# hi\n")
    c = HealthChecker()
    out = asyncio.run(c.check_wiki())
    assert out["status"] == "ok"


def test_check_wiki_broken_missing_index(isolated_home):
    c = HealthChecker()
    out = asyncio.run(c.check_wiki())
    assert out["status"] == "broken"


def test_check_goals_ok(isolated_home):
    _, wiki = isolated_home
    (wiki / "goals.md").write_text("# goals\n")
    c = HealthChecker()
    out = asyncio.run(c.check_goals())
    assert out["status"] == "ok"


def test_check_goals_degraded_when_missing(isolated_home):
    c = HealthChecker()
    out = asyncio.run(c.check_goals())
    assert out["status"] == "degraded"


def test_check_recent_errors_ok(isolated_home):
    c = HealthChecker()
    out = asyncio.run(c.check_recent_errors())
    assert out["status"] == "ok"


def test_check_recent_errors_degraded(isolated_home):
    home, _ = isolated_home
    errors = home / "errors.jsonl"
    for _ in range(3):
        _append_jsonl(errors, {"timestamp": _now_iso_offset(60), "code": "x"})
    c = HealthChecker()
    out = asyncio.run(c.check_recent_errors())
    assert out["status"] == "degraded"
    assert "3 errors" in out["detail"]


def test_check_recent_errors_broken(isolated_home):
    home, _ = isolated_home
    errors = home / "errors.jsonl"
    for _ in range(7):
        _append_jsonl(errors, {"timestamp": _now_iso_offset(60), "code": "x"})
    c = HealthChecker()
    out = asyncio.run(c.check_recent_errors())
    assert out["status"] == "broken"


def test_check_recent_errors_ignores_old_rows(isolated_home):
    home, _ = isolated_home
    errors = home / "errors.jsonl"
    for _ in range(10):
        _append_jsonl(errors, {"timestamp": _now_iso_offset(60 * 60 * 2), "code": "x"})
    c = HealthChecker()
    out = asyncio.run(c.check_recent_errors())
    assert out["status"] == "ok"


def test_check_integrate_ok(isolated_home):
    home, _ = isolated_home
    audit = home / "audit.jsonl"
    _append_jsonl(audit, {
        "ts": _now_iso_offset(60),
        "action": "wiki_write",
        "target": "foo",
        "reason": "r",
    })
    c = HealthChecker()
    out = asyncio.run(c.check_integrate())
    assert out["status"] == "ok"


def test_check_integrate_degraded(isolated_home):
    home, _ = isolated_home
    audit = home / "audit.jsonl"
    _append_jsonl(audit, {
        "ts": _now_iso_offset(60 * 25),
        "action": "cycle_no_op",
        "target": "foo",
        "reason": "r",
    })
    c = HealthChecker()
    out = asyncio.run(c.check_integrate())
    assert out["status"] == "degraded"


def test_check_integrate_broken_when_empty(isolated_home):
    c = HealthChecker()
    out = asyncio.run(c.check_integrate())
    assert out["status"] == "broken"


def test_check_integrate_broken_when_stale(isolated_home):
    home, _ = isolated_home
    audit = home / "audit.jsonl"
    _append_jsonl(audit, {
        "ts": _now_iso_offset(60 * 60 * 2),
        "action": "wiki_write",
        "target": "foo",
        "reason": "r",
    })
    c = HealthChecker()
    out = asyncio.run(c.check_integrate())
    assert out["status"] == "broken"


# ---------------------------------------------------------------------------
# full run() integration
# ---------------------------------------------------------------------------


def test_run_produces_seven_checks_and_writes_file(isolated_home, monkeypatch):
    home, wiki = isolated_home
    _patch_httpx(monkeypatch, status=200)
    # Seed a happy-path fixture.
    (wiki / "index.md").write_text("# idx\n")
    (wiki / "goals.md").write_text("# goals\n")
    _write_screen_ts(home, 5)
    _append_jsonl(home / "observations.jsonl", {
        "ts": _now_iso_offset(30), "kind": "x",
    })
    _append_jsonl(home / "audit.jsonl", {
        "ts": _now_iso_offset(60), "action": "wiki_write",
        "target": "t", "reason": "r",
    })

    payload = asyncio.run(HealthChecker().run())
    ids = [c["id"] for c in payload["checks"]]
    assert set(ids) == {
        "proxy", "recent_signals", "latest_screen",
        "wiki", "goals", "recent_errors", "integrate",
    }
    assert payload["overall"] == "ok"
    assert payload["app_version"]
    assert "timestamp" in payload

    # File round-trips as valid JSON.
    disk = json.loads((home / "health.json").read_text())
    assert disk["overall"] == "ok"
    assert len(disk["checks"]) == 7


def test_run_overall_broken_when_proxy_down(isolated_home, monkeypatch):
    home, wiki = isolated_home
    _patch_httpx(monkeypatch, status=503)
    (wiki / "index.md").write_text("# idx\n")
    (wiki / "goals.md").write_text("# goals\n")
    _write_screen_ts(home, 5)
    _append_jsonl(home / "observations.jsonl", {
        "ts": _now_iso_offset(30), "kind": "x",
    })
    _append_jsonl(home / "audit.jsonl", {
        "ts": _now_iso_offset(60), "action": "wiki_write",
        "target": "t", "reason": "r",
    })

    payload = asyncio.run(HealthChecker().run())
    assert payload["overall"] == "broken"
    proxy = next(c for c in payload["checks"] if c["id"] == "proxy")
    assert proxy["status"] == "broken"


def test_run_bubbles_last_error_request_id(isolated_home, monkeypatch):
    home, _ = isolated_home
    _patch_httpx(monkeypatch, status=200)
    _append_jsonl(home / "errors.jsonl", {
        "request_id": "req_aaa111bbb222",
        "timestamp": _now_iso_offset(60),
        "code": "proxy_unavailable",
    })
    payload = asyncio.run(HealthChecker().run())
    assert payload["last_error_request_id"] == "req_aaa111bbb222"


def test_run_all_fields_json_serializable(isolated_home, monkeypatch):
    _patch_httpx(monkeypatch, status=200)
    payload = asyncio.run(HealthChecker().run())
    # Must round-trip with no custom encoder.
    reserialized = json.loads(json.dumps(payload))
    assert reserialized == payload
    for check in payload["checks"]:
        assert set(check.keys()) >= {
            "id", "label", "status", "detail", "fix", "fix_url",
        }
        assert check["status"] in ("ok", "degraded", "broken")
        assert len(check["detail"]) <= 120
