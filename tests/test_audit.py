"""audit.jsonl single-source-of-truth contract.

Every agent mutation now flows through ``deja.audit.record``. These
tests lock in the schema so the notch Activity tab, the Swift
``DatabaseReader.readInsights`` adapter, and any future ``jq``-based
debugging can rely on the same shape.
"""

from __future__ import annotations

import json

import pytest

from deja import audit


def _read_audit(home) -> list[dict]:
    path = home / "audit.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_record_writes_one_line_per_call(isolated_home):
    home, _ = isolated_home
    audit.record("wiki_write", target="people/amanda-peffer", reason="first touch")
    audit.record("task_add", target="goals/tasks", reason="call memere")
    audit.record("reminder_add", target="goals/reminders", reason="check in a week")

    entries = _read_audit(home)
    assert len(entries) == 3
    assert [e["action"] for e in entries] == ["wiki_write", "task_add", "reminder_add"]
    assert [e["target"] for e in entries] == [
        "people/amanda-peffer", "goals/tasks", "goals/reminders",
    ]
    assert [e["reason"] for e in entries] == [
        "first touch", "call memere", "check in a week",
    ]


def test_record_schema_required_fields(isolated_home):
    """Every entry must have ts, cycle, trigger, action, target, reason.

    If any of these go missing the downstream consumers break silently
    — DatabaseReader.readInsights drops the row, the /api/activity
    endpoint returns malformed entries, etc.
    """
    home, _ = isolated_home
    audit.record("task_complete", target="goals/tasks", reason="sent the email")
    entry = _read_audit(home)[0]

    required = {"ts", "cycle", "trigger", "action", "target", "reason"}
    missing = required - set(entry.keys())
    assert not missing, f"audit entry missing required fields: {missing}"

    # trigger must be a dict with kind + detail
    assert isinstance(entry["trigger"], dict)
    assert "kind" in entry["trigger"]
    assert "detail" in entry["trigger"]

    # ts must be ISO8601 with Z suffix
    assert entry["ts"].endswith("Z")
    assert "T" in entry["ts"]


def test_context_propagates_through_record(isolated_home):
    """``set_context`` sets cycle + trigger for all subsequent records.

    This is how analysis_cycle threads cycle_id + trigger_kind through
    every downstream mutation without passing args through every helper.
    """
    home, _ = isolated_home
    audit.set_context("c_test123", "reminder", "amanda check")
    try:
        audit.record("wiki_write", target="people/amanda", reason="fix stale claim")
        audit.record("reminder_resolve", target="goals/reminders", reason="done")
    finally:
        audit.clear_context()

    entries = _read_audit(home)
    for e in entries:
        assert e["cycle"] == "c_test123"
        assert e["trigger"]["kind"] == "reminder"
        assert e["trigger"]["detail"] == "amanda check"


def test_context_cleared_between_cycles(isolated_home):
    home, _ = isolated_home

    audit.set_context("c_one", "signal", "tick")
    audit.record("wiki_write", target="people/a", reason="from cycle one")

    audit.clear_context()
    audit.record("wiki_write", target="people/b", reason="from outside any cycle")

    audit.set_context("c_two", "reminder", "due")
    audit.record("wiki_write", target="people/c", reason="from cycle two")
    audit.clear_context()

    entries = _read_audit(home)
    assert entries[0]["cycle"] == "c_one"
    assert entries[1]["cycle"] == ""  # no cycle ⇒ admin write
    assert entries[2]["cycle"] == "c_two"


def test_explicit_cycle_and_trigger_override_context(isolated_home):
    home, _ = isolated_home
    audit.set_context("c_ambient", "signal", "ambient")
    try:
        audit.record(
            "dedup_merge",
            target="wiki/*",
            reason="merged two pages",
            cycle="c_dedup456",
            trigger={"kind": "dedup", "detail": "scheduled"},
        )
    finally:
        audit.clear_context()

    entry = _read_audit(home)[0]
    assert entry["cycle"] == "c_dedup456"
    assert entry["trigger"]["kind"] == "dedup"


def test_new_cycle_id_is_unique_and_short():
    ids = {audit.new_cycle_id() for _ in range(200)}
    assert len(ids) == 200, "cycle_id generator produced collisions"
    for cid in list(ids)[:10]:
        assert cid.startswith("c_")
        assert len(cid) < 32


def test_read_recent_returns_newest_first(isolated_home):
    home, _ = isolated_home
    audit.record("wiki_write", target="a", reason="first")
    audit.record("wiki_write", target="b", reason="second")
    audit.record("wiki_write", target="c", reason="third")

    recent = audit.read_recent(limit=10)
    assert len(recent) == 3
    assert recent[0]["target"] == "c"  # newest first
    assert recent[2]["target"] == "a"


def test_read_recent_filters_by_trigger_kind(isolated_home):
    home, _ = isolated_home
    audit.record("wiki_write", target="a", reason="r1",
                 trigger={"kind": "signal", "detail": "tick"})
    audit.record("wiki_write", target="b", reason="r2",
                 trigger={"kind": "reminder", "detail": "due"})
    audit.record("wiki_write", target="c", reason="r3",
                 trigger={"kind": "signal", "detail": "tick"})

    only_reminders = audit.read_recent(limit=10, kind="reminder")
    assert len(only_reminders) == 1
    assert only_reminders[0]["target"] == "b"


def test_record_failure_never_raises(isolated_home, monkeypatch):
    """audit.record must be fire-and-forget. A disk error must not
    break the agent cycle — it just logs the exception internally."""
    home, _ = isolated_home
    # Point AUDIT_LOG at an unwritable path
    monkeypatch.setattr(audit, "AUDIT_LOG", home / "nope" / "nope" / "audit.jsonl")
    # Remove mkdir permissions by using a file where a dir is expected
    (home / "nope").write_text("blocking file")

    # Should not raise
    audit.record("wiki_write", target="test", reason="should not blow up")
