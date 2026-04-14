"""Tests for the observability package: context, typed errors, reporter."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import httpx
import pytest

from deja.observability import (
    AuthError,
    DejaError,
    LLMError,
    ProxyUnavailable,
    RateLimitError,
    RequestIDLogFilter,
    current_request_id,
    new_request_id,
    report_error,
    request_scope,
)


# ---------------------------------------------------------------------------
# context.py
# ---------------------------------------------------------------------------


def test_request_scope_sets_and_clears():
    assert current_request_id() is None
    with request_scope() as rid:
        assert rid.startswith("req_")
        assert len(rid) == len("req_") + 12
        assert current_request_id() == rid
    assert current_request_id() is None


def test_request_scope_nests_and_restores():
    with request_scope() as outer:
        assert current_request_id() == outer
        with request_scope() as inner:
            assert inner != outer
            assert current_request_id() == inner
        assert current_request_id() == outer
    assert current_request_id() is None


def test_new_request_id_binds():
    assert current_request_id() is None
    with request_scope():
        rid = new_request_id()
        assert rid.startswith("req_")
        assert current_request_id() == rid


def test_log_filter_adds_prefix_when_active(caplog):
    log = logging.getLogger("deja.test.obs.filter")
    log.setLevel(logging.DEBUG)
    # Ensure the root filter is attached (it is at import time).
    root = logging.getLogger()
    assert any(isinstance(f, RequestIDLogFilter) for f in root.filters)

    # Directly exercise the filter to verify it mutates records.
    rec = logging.LogRecord(
        "n", logging.INFO, "f", 1, "msg", None, None,
    )
    filt = RequestIDLogFilter()
    filt.filter(rec)
    assert rec.request_id == ""
    assert rec.req_prefix == ""

    with request_scope() as rid:
        rec2 = logging.LogRecord(
            "n", logging.INFO, "f", 1, "msg", None, None,
        )
        filt.filter(rec2)
        assert rec2.request_id == rid
        assert rec2.req_prefix == f"[{rid}] "


# ---------------------------------------------------------------------------
# errors.py
# ---------------------------------------------------------------------------


def test_errors_capture_current_request_id():
    with request_scope() as rid:
        err = ProxyUnavailable("boom", details={"http_status": 502})
        assert err.request_id == rid
        assert err.code == "proxy_unavailable"
        assert "couldn't reach" in err.user_message.lower()
        assert err.details["http_status"] == 502


def test_error_outside_scope_has_no_request_id():
    err = LLMError("boom")
    assert err.request_id is None
    assert err.code == "llm_error"


def test_error_types_have_stable_codes():
    assert ProxyUnavailable().code == "proxy_unavailable"
    assert AuthError().code == "auth_failed"
    assert RateLimitError().code == "rate_limited"
    assert LLMError().code == "llm_error"


def test_to_user_error_file_writes_atomically(isolated_home):
    home, _wiki = isolated_home
    with request_scope():
        err = ProxyUnavailable(
            "boom", details={"http_status": 502, "url": "https://x/y"},
        )
        path = err.to_user_error_file()
    assert path == Path(home) / "latest_error.json"
    payload = json.loads(path.read_text())
    assert payload["code"] == "proxy_unavailable"
    assert payload["request_id"] == err.request_id
    assert payload["details"]["http_status"] == 502
    assert "timestamp" in payload
    assert payload["timestamp"].endswith("Z")

    # No leftover tmp files.
    leftovers = [p for p in Path(home).iterdir() if p.name.startswith(".latest_error.")]
    assert leftovers == []


# ---------------------------------------------------------------------------
# reporter.py
# ---------------------------------------------------------------------------


def test_report_error_writes_both_sinks(isolated_home):
    home, _ = isolated_home
    with request_scope():
        err = ProxyUnavailable(
            "boom",
            details={"http_status": 502, "url": "https://proxy/x"},
        )
        report_error(err, visible_to_user=True)

    latest = Path(home) / "latest_error.json"
    errors_log = Path(home) / "errors.jsonl"
    assert latest.exists()
    assert errors_log.exists()

    latest_payload = json.loads(latest.read_text())
    log_lines = errors_log.read_text().strip().splitlines()
    assert len(log_lines) == 1
    log_payload = json.loads(log_lines[0])
    assert log_payload["code"] == latest_payload["code"] == "proxy_unavailable"
    assert log_payload["request_id"] == latest_payload["request_id"]


def test_report_error_invisible_only_appends_log(isolated_home):
    home, _ = isolated_home
    latest = Path(home) / "latest_error.json"
    errors_log = Path(home) / "errors.jsonl"
    assert not latest.exists()

    err = LLMError("boom", details={"x": 1})
    report_error(err, visible_to_user=False)

    assert not latest.exists(), "invisible error must not touch latest_error.json"
    assert errors_log.exists()
    assert len(errors_log.read_text().strip().splitlines()) == 1


def test_report_error_appends_multiple_lines(isolated_home):
    home, _ = isolated_home
    errors_log = Path(home) / "errors.jsonl"
    for i in range(3):
        report_error(
            LLMError(f"boom {i}", details={"i": i}), visible_to_user=False,
        )
    assert len(errors_log.read_text().strip().splitlines()) == 3


# ---------------------------------------------------------------------------
# llm_client integration: 502 → ProxyUnavailable with request_id
# ---------------------------------------------------------------------------


def _make_fake_client(monkeypatch, fake_http):
    from deja import llm_client

    monkeypatch.setattr(llm_client, "_USE_DIRECT", False)
    monkeypatch.setattr(
        "deja.auth.get_auth_token", lambda: "test-token", raising=False,
    )
    client = llm_client.GeminiClient.__new__(llm_client.GeminiClient)
    client._direct_client = None
    client._http = fake_http
    return client


def test_llm_client_502_raises_proxy_unavailable(monkeypatch):
    """Simulate a 502 from the proxy and assert ProxyUnavailable is raised
    with the active request id attached.
    """
    class R:
        status_code = 502
        text = "bad gateway"

    class FakeHttp:
        async def post(self, *a, **kw):
            return R()

    client = _make_fake_client(monkeypatch, FakeHttp())

    async def run():
        with request_scope() as rid:
            try:
                await client._generate("m", "hi", {})
                return rid, None
            except ProxyUnavailable as e:
                return rid, e

    rid, err = asyncio.run(run())
    assert err is not None, "expected ProxyUnavailable"
    assert err.code == "proxy_unavailable"
    assert err.request_id == rid
    assert err.details["http_status"] == 502


def test_llm_client_timeout_raises_proxy_unavailable(monkeypatch):
    class FakeHttp:
        async def post(self, *a, **kw):
            raise httpx.ConnectError("connection refused")

    client = _make_fake_client(monkeypatch, FakeHttp())

    async def run():
        with request_scope():
            try:
                await client._generate("m", "hi", {})
                return None
            except ProxyUnavailable as e:
                return e

    err = asyncio.run(run())
    assert err is not None


def test_llm_client_401_raises_auth_error(monkeypatch):
    class R:
        status_code = 401
        text = "nope"

    class FakeHttp:
        async def post(self, *a, **kw):
            return R()

    client = _make_fake_client(monkeypatch, FakeHttp())

    async def run():
        with request_scope():
            try:
                await client._generate("m", "hi", {})
                return None
            except AuthError as e:
                return e

    assert asyncio.run(run()) is not None


def test_llm_client_429_raises_rate_limit(monkeypatch):
    class R:
        status_code = 429
        text = "slow down"

    class FakeHttp:
        async def post(self, *a, **kw):
            return R()

    client = _make_fake_client(monkeypatch, FakeHttp())

    async def run():
        with request_scope():
            try:
                await client._generate("m", "hi", {})
                return None
            except RateLimitError as e:
                return e

    assert asyncio.run(run()) is not None


# ---------------------------------------------------------------------------
# audit.py — auto-includes request_id on new rows
# ---------------------------------------------------------------------------


def test_audit_record_includes_request_id(isolated_home):
    home, _ = isolated_home
    from deja import audit

    with request_scope() as rid:
        audit.record("wiki_write", "people/x", "testing")

    lines = (Path(home) / "audit.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["request_id"] == rid
    assert entry["action"] == "wiki_write"


def test_audit_record_without_scope_omits_request_id(isolated_home):
    home, _ = isolated_home
    from deja import audit

    audit.record("wiki_write", "people/x", "testing")
    lines = (Path(home) / "audit.jsonl").read_text().strip().splitlines()
    entry = json.loads(lines[0])
    assert "request_id" not in entry
