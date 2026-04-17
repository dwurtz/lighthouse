"""Regression tests for the stale-screenshot filter.

Backstop against the timezone bug fixed in commit 5bd67e9: observation
timestamps are naive LOCAL, but a prior version of
``filter_stale_screenshots`` compared them against aware-UTC now after
stamping them with ``tzinfo=timezone.utc``. For users not running in
UTC, every screenshot appeared ``UTC_offset`` hours older than reality
and was silently dropped — integrate was blind to current screen
context for six days before we caught it.

These tests pin the invariant: a fresh screenshot (``datetime.now()``
at call time) must always survive the filter, regardless of the
machine's timezone.
"""
from datetime import datetime, timedelta, timezone

from deja.agent.analysis_cycle import (
    MAX_SCREENSHOT_AGE_SECONDS,
    filter_stale_screenshots,
)


def _screenshot(ts: datetime, text: str = "[Focused: Test — \"x\"]") -> dict:
    return {
        "source": "screenshot",
        "sender": "display-1",
        "text": text,
        "timestamp": ts.isoformat(),
        "id_key": "test",
    }


def test_fresh_screenshot_is_kept():
    """A screenshot with a NOW timestamp must survive the filter.

    This is the invariant that was silently broken by mixing naive
    local with aware UTC. If this assertion fails on any timezone,
    integrate has gone blind to current-screen context.
    """
    fresh = _screenshot(datetime.now())
    kept = filter_stale_screenshots([fresh])
    assert kept == [fresh], "fresh screenshot was wrongly filtered"


def test_recent_screenshot_is_kept():
    """A 5-minute-old screenshot (well under the 30-min cap) stays."""
    recent = _screenshot(datetime.now() - timedelta(minutes=5))
    kept = filter_stale_screenshots([recent])
    assert kept == [recent]


def test_stale_screenshot_is_dropped():
    """A screenshot older than MAX_SCREENSHOT_AGE_SECONDS is filtered."""
    ancient = _screenshot(
        datetime.now() - timedelta(seconds=MAX_SCREENSHOT_AGE_SECONDS + 120)
    )
    kept = filter_stale_screenshots([ancient])
    assert kept == []


def test_non_screenshot_sources_pass_through_unfiltered():
    """Email, iMessage, etc. are never dropped by the screenshot filter —
    even with a very old timestamp."""
    old_email = {
        "source": "email",
        "sender": "someone",
        "text": "hello",
        "timestamp": "2020-01-01T00:00:00",
        "id_key": "old-email",
    }
    kept = filter_stale_screenshots([old_email])
    assert kept == [old_email]


def test_aware_utc_timestamps_are_normalized_before_compare():
    """Defense: if a collector ever starts writing aware-UTC timestamps,
    the filter must convert them to naive local before computing age,
    not subtract across timezones.
    """
    # An aware-UTC timestamp representing "right now"
    now_utc = datetime.now(timezone.utc)
    aware = {
        "source": "screenshot",
        "sender": "display-1",
        "text": "[Focused: X]",
        "timestamp": now_utc.isoformat(),
        "id_key": "aware",
    }
    kept = filter_stale_screenshots([aware])
    assert kept == [aware], (
        "aware-UTC timestamp representing 'now' must survive the filter"
    )


def test_empty_input_returns_empty():
    assert filter_stale_screenshots([]) == []


def test_unparseable_timestamp_is_kept():
    """Garbage timestamps shouldn't silently drop the signal — let
    downstream surface the failure."""
    broken = {
        "source": "screenshot",
        "sender": "display-1",
        "text": "[Focused: X]",
        "timestamp": "not-a-date",
        "id_key": "broken",
    }
    kept = filter_stale_screenshots([broken])
    assert kept == [broken]


def test_mixed_batch_preserves_order():
    """The filter must not reorder its input — audit-id threading and
    format rendering depend on stable order."""
    items = [
        {"source": "email", "sender": "a", "text": "e", "timestamp": "2020-01-01T00:00:00", "id_key": "e1"},
        _screenshot(datetime.now(), text="[Focused: fresh]"),
        {"source": "imessage", "sender": "b", "text": "i", "timestamp": "2020-01-01T00:00:00", "id_key": "i1"},
        _screenshot(datetime.now() - timedelta(seconds=MAX_SCREENSHOT_AGE_SECONDS + 120), text="[Focused: stale]"),
        {"source": "browser", "sender": "c", "text": "b", "timestamp": "2020-01-01T00:00:00", "id_key": "b1"},
    ]
    kept = filter_stale_screenshots(items)
    # Index 3 is the stale screenshot — it should be the only drop.
    assert [o["id_key"] for o in kept] == ["e1", "test", "i1", "b1"]
