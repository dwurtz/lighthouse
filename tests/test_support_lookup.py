"""Support-side request-id lookup tool.

Exercises ``tools/deja_support_lookup.py`` end-to-end: seed fixture
files under an isolated ``~/.deja``, then invoke the tool as a module
and assert its output reconstructs the timeline we planted.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

# Make tools/ importable as a package path for direct function calls.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import deja_support_lookup as lookup  # noqa: E402


RID = "req_abc123def456"
OTHER_RID = "req_zzzzzzzzzzzz"


def _seed(home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)

    # deja.log: one matching line, one not
    (home / "deja.log").write_text(
        f"2026-04-11 12:00:00,123 deja.agent INFO [{RID}] starting cycle\n"
        f"2026-04-11 12:00:05,000 deja.other INFO unrelated line\n"
        f"2026-04-11 12:00:06,000 deja.agent INFO [{OTHER_RID}] other request\n"
        f"2026-04-11 12:00:10,555 deja.agent ERROR [{RID}] llm call failed\n",
        encoding="utf-8",
    )

    # audit.jsonl: one matching, one without request_id
    rows = [
        {
            "ts": "2026-04-11T12:00:02Z",
            "cycle": "c_xyz",
            "trigger": {"kind": "signal", "detail": "tick"},
            "action": "wiki_write",
            "target": "projects/tru",
            "reason": "updated tru status",
            "request_id": RID,
        },
        {
            "ts": "2026-04-11T12:00:03Z",
            "cycle": "c_xyz",
            "trigger": {"kind": "signal", "detail": ""},
            "action": "task_add",
            "target": "goals/tasks",
            "reason": "added task — no request id here",
        },
        {
            "ts": "2026-04-11T12:00:04Z",
            "cycle": "c_other",
            "trigger": {"kind": "signal"},
            "action": "wiki_write",
            "target": "people/other",
            "reason": "other request",
            "request_id": OTHER_RID,
        },
    ]
    with (home / "audit.jsonl").open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")

    # errors.jsonl: one matching
    errs = [
        {
            "request_id": RID,
            "code": "llm_error",
            "message": "Gemini 500",
            "timestamp": "2026-04-11T12:00:11Z",
            "details": {"status": 500},
        },
        {
            "request_id": OTHER_RID,
            "code": "proxy_unavailable",
            "message": "Render down",
            "timestamp": "2026-04-11T12:00:12Z",
            "details": {},
        },
    ]
    with (home / "errors.jsonl").open("w", encoding="utf-8") as f:
        for r in errs:
            f.write(json.dumps(r) + "\n")


# ---------------------------------------------------------------------------
# In-process tests (fast, direct function calls)
# ---------------------------------------------------------------------------

def test_lookup_finds_all_three_sources(isolated_home):
    home, _ = isolated_home
    _seed(home)

    events = lookup.lookup(RID, base=home)

    sources = [e.source for e in events]
    assert "LOG" in sources
    assert "AUDIT" in sources
    assert "ERROR" in sources

    # Both matching log lines picked up (start + error).
    log_events = [e for e in events if e.source == "LOG"]
    assert len(log_events) == 2
    assert all(RID in e.summary for e in log_events)

    # Exactly one audit row.
    audit_events = [e for e in events if e.source == "AUDIT"]
    assert len(audit_events) == 1
    assert audit_events[0].raw["target"] == "projects/tru"

    # Exactly one error row.
    err_events = [e for e in events if e.source == "ERROR"]
    assert len(err_events) == 1
    assert err_events[0].raw["code"] == "llm_error"


def test_lookup_excludes_unrelated_rows(isolated_home):
    home, _ = isolated_home
    _seed(home)

    events = lookup.lookup(RID, base=home)
    for e in events:
        # No event should reference the other request id.
        assert OTHER_RID not in e.summary
        if isinstance(e.raw, dict):
            assert e.raw.get("request_id") != OTHER_RID


def test_lookup_missing_rid_returns_empty(isolated_home):
    home, _ = isolated_home
    _seed(home)
    assert lookup.lookup("req_nope", base=home) == []


def test_since_filter(isolated_home):
    home, _ = isolated_home
    _seed(home)
    # Cut off before the error at 12:00:10 — we should only get events
    # at or after that.
    events = lookup.lookup(RID, base=home, since="2026-04-11T12:00:10")
    assert events
    assert all(e.sort_key >= "2026-04-11T12:00:10" for e in events)


# ---------------------------------------------------------------------------
# Subprocess tests — exercise argv + exit code + stdout end-to-end.
# ---------------------------------------------------------------------------

def _run_cli(home: Path, *args: str) -> subprocess.CompletedProcess:
    script = REPO_ROOT / "tools" / "deja_support_lookup.py"
    return subprocess.run(
        [sys.executable, str(script), *args, "--path", str(home)],
        capture_output=True,
        text=True,
    )


def test_cli_prose_output(isolated_home):
    home, _ = isolated_home
    _seed(home)

    proc = _run_cli(home, RID)
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert f"timeline for {RID}" in out
    assert "LOG" in out
    assert "AUDIT" in out
    assert "ERROR" in out
    assert "projects/tru" in out
    assert "llm_error" in out
    # Unrelated line should not appear.
    assert "unrelated line" not in out
    assert OTHER_RID not in out


def test_cli_json_output_is_valid_json(isolated_home):
    home, _ = isolated_home
    _seed(home)

    proc = _run_cli(home, RID, "--json")
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["request_id"] == RID
    assert payload["event_count"] == len(payload["events"])
    assert payload["event_count"] >= 4  # 2 log + 1 audit + 1 error
    sources = {e["source"] for e in payload["events"]}
    assert sources == {"LOG", "AUDIT", "ERROR"}


def test_cli_exit_1_when_no_trace(isolated_home):
    home, _ = isolated_home
    _seed(home)

    proc = _run_cli(home, "req_nothing_here")
    assert proc.returncode == 1
