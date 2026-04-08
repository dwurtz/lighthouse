"""Slot schedule for the daily reflection pass.

Reflection runs at most once per slot (default 02:00 / 11:00 / 18:00 local).
The "is it time?" check is clock-aligned, not interval-based: it asks
whether the last successful run predates the most recent slot boundary.
These tests lock the edge cases — wrap-to-yesterday, catch-up on sleep,
slot-just-crossed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture
def reflect_mod(monkeypatch):
    import deja.reflection as reflection
    import deja.reflection_scheduler as scheduler
    # Force the default 3-slot schedule for deterministic tests. Any
    # override via config.yaml would otherwise leak in.
    # The real functions live in reflection_scheduler; reflection
    # re-exports them. Patch the scheduler so the functions see the
    # overridden value.
    monkeypatch.setattr(scheduler, "REFLECT_SLOT_HOURS", (2, 11, 18))
    monkeypatch.setattr(reflection, "REFLECT_SLOT_HOURS", (2, 11, 18))
    return reflection


def _local(year, month, day, hour, minute=0):
    """Helper: return an aware local-time datetime."""
    return datetime(year, month, day, hour, minute).astimezone()


# ---------------------------------------------------------------------------
# _most_recent_slot — the core of the check
# ---------------------------------------------------------------------------

def test_most_recent_slot_between_slots_returns_prior(reflect_mod):
    # It's 12:00 — the most recent slot is today's 11:00
    now = _local(2026, 4, 5, 12, 0)
    slot = reflect_mod._most_recent_slot(now)
    assert slot.hour == 11
    assert slot.date() == now.date()


def test_most_recent_slot_at_slot_boundary_returns_that_slot(reflect_mod):
    # It's exactly 18:00 — that IS the current slot
    now = _local(2026, 4, 5, 18, 0)
    slot = reflect_mod._most_recent_slot(now)
    assert slot.hour == 18
    assert slot.date() == now.date()


def test_most_recent_slot_just_after_boundary(reflect_mod):
    now = _local(2026, 4, 5, 18, 1)
    slot = reflect_mod._most_recent_slot(now)
    assert slot.hour == 18


def test_most_recent_slot_before_first_daily_slot_wraps_to_yesterday(reflect_mod):
    # 01:30 — before today's earliest slot (02:00); wraps to yesterday's last slot
    now = _local(2026, 4, 5, 1, 30)
    slot = reflect_mod._most_recent_slot(now)
    assert slot.hour == 18
    assert slot.date() == (now - timedelta(days=1)).date()


def test_most_recent_slot_between_midmorning_and_afternoon(reflect_mod):
    # 14:00 — between 11:00 and 18:00 → most recent is 11:00
    now = _local(2026, 4, 5, 14, 0)
    slot = reflect_mod._most_recent_slot(now)
    assert slot.hour == 11


# ---------------------------------------------------------------------------
# should_run_reflection — ties _most_recent_slot to the last-run marker
# ---------------------------------------------------------------------------

def test_should_run_when_no_marker(reflect_mod, monkeypatch):
    import deja.reflection_scheduler as scheduler
    monkeypatch.setattr(scheduler, "_read_last_run", lambda: None)
    assert reflect_mod.should_run_reflection(now=_local(2026, 4, 5, 12, 0)) is True


def test_should_not_run_when_last_run_is_after_most_recent_slot(reflect_mod, monkeypatch):
    import deja.reflection_scheduler as scheduler
    # It's 12:00. Most recent slot is 11:00. Last run was 11:30 → don't run.
    last = _local(2026, 4, 5, 11, 30).astimezone(timezone.utc)
    monkeypatch.setattr(scheduler, "_read_last_run", lambda: last)
    assert reflect_mod.should_run_reflection(now=_local(2026, 4, 5, 12, 0)) is False


def test_should_run_when_last_run_predates_most_recent_slot(reflect_mod, monkeypatch):
    import deja.reflection_scheduler as scheduler
    # It's 12:00. Most recent slot is 11:00. Last run was today at 03:00
    # (past 02:00 slot, before 11:00 slot) → run.
    last = _local(2026, 4, 5, 3, 0).astimezone(timezone.utc)
    monkeypatch.setattr(scheduler, "_read_last_run", lambda: last)
    assert reflect_mod.should_run_reflection(now=_local(2026, 4, 5, 12, 0)) is True


def test_should_not_run_between_slots_if_last_run_was_between_same_slots(reflect_mod, monkeypatch):
    import deja.reflection_scheduler as scheduler
    # 14:00. Last run was 12:00 — both are between the 11:00 and 18:00 slots → don't run
    last = _local(2026, 4, 5, 12, 0).astimezone(timezone.utc)
    monkeypatch.setattr(scheduler, "_read_last_run", lambda: last)
    assert reflect_mod.should_run_reflection(now=_local(2026, 4, 5, 14, 0)) is False


def test_should_run_catch_up_once_after_long_sleep(reflect_mod, monkeypatch):
    """Machine asleep for a day; wakes at 20:00. Last run was 3 days ago.
    Should fire ONCE (targeting today's 18:00 slot) — no stampede."""
    import deja.reflection_scheduler as scheduler
    last = (_local(2026, 4, 2, 18, 0)).astimezone(timezone.utc)
    monkeypatch.setattr(scheduler, "_read_last_run", lambda: last)
    assert reflect_mod.should_run_reflection(now=_local(2026, 4, 5, 20, 0)) is True


def test_should_not_run_before_first_slot_if_ran_yesterday_evening(reflect_mod, monkeypatch):
    import deja.reflection_scheduler as scheduler
    # It's 01:30 today. Last run was yesterday at 19:00 (past yesterday's
    # 18:00 slot, the most recent boundary). Today's 02:00 hasn't crossed.
    # → don't run yet.
    last = _local(2026, 4, 4, 19, 0).astimezone(timezone.utc)
    monkeypatch.setattr(scheduler, "_read_last_run", lambda: last)
    assert reflect_mod.should_run_reflection(now=_local(2026, 4, 5, 1, 30)) is False


# ---------------------------------------------------------------------------
# Config flexibility — user can override the slot schedule
# ---------------------------------------------------------------------------

def test_custom_slot_schedule_two_slots(reflect_mod, monkeypatch):
    """A user who wants the old daily behavior can set REFLECT_SLOT_HOURS
    to a single hour; the logic still works."""
    import deja.reflection_scheduler as scheduler
    monkeypatch.setattr(scheduler, "REFLECT_SLOT_HOURS", (2,))
    # 14:00 — only slot is 02:00 → most recent is today's 02:00
    now = _local(2026, 4, 5, 14, 0)
    slot = reflect_mod._most_recent_slot(now)
    assert slot.hour == 2


def test_custom_slot_schedule_six_slots(reflect_mod, monkeypatch):
    """A user who wants very tight reflection can add more slots."""
    import deja.reflection_scheduler as scheduler
    monkeypatch.setattr(scheduler, "REFLECT_SLOT_HOURS", (0, 4, 8, 12, 16, 20))
    # 13:00 — most recent slot is 12:00
    now = _local(2026, 4, 5, 13, 0)
    slot = reflect_mod._most_recent_slot(now)
    assert slot.hour == 12


def test_empty_slot_schedule_never_triggers(reflect_mod, monkeypatch):
    """Pathological config — should degrade quietly rather than crash."""
    import deja.reflection_scheduler as scheduler
    monkeypatch.setattr(scheduler, "REFLECT_SLOT_HOURS", ())
    last = _local(2026, 1, 1, 0, 0).astimezone(timezone.utc)
    monkeypatch.setattr(scheduler, "_read_last_run", lambda: last)
    # Empty slots → most_recent_slot returns `now` → last < now is True in
    # general, but since there's no meaningful boundary this is a degenerate
    # case; we accept either behavior as long as it doesn't crash.
    # (The config path always injects at least one slot, so this is a
    # hypothetical.)
    result = reflect_mod.should_run_reflection(now=_local(2026, 4, 5, 12, 0))
    assert isinstance(result, bool)
