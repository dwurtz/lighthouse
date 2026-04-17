"""Regression tests for observation timestamp parsing.

Backs the invariant that prevents the recurring naive-local/aware-UTC
mismatch bug class. See
``deja.observations.time_utils.parse_observation_ts`` for the rule
book. Each collector writes naive-local; the audit log writes aware
UTC; downstream code compares against ``datetime.now(timezone.utc)``.
This helper is the one place that conversion happens correctly.
"""
from datetime import datetime, timedelta, timezone

import pytest

from deja.observations.time_utils import parse_observation_ts


def test_naive_local_is_treated_as_local_and_converted_to_utc():
    """A naive-local ISO string (no tzinfo, representing local wall
    clock) must be interpreted as local time and returned as aware UTC.

    This is THE invariant. If this test fails, non-UTC users will see
    silent bugs: watchdog false-positives, stale-screenshot drops,
    incorrect "last signal N minutes ago" UIs. Do not change the
    assertion direction without auditing every caller.
    """
    # Construct a naive-local "now" by formatting datetime.now() without tz.
    naive_now = datetime.now().replace(microsecond=0)
    ts_str = naive_now.isoformat()

    parsed = parse_observation_ts(ts_str)

    # Must be aware UTC
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)

    # Must represent approximately the same instant as datetime.now(utc)
    now_utc = datetime.now(timezone.utc)
    delta = abs((now_utc - parsed).total_seconds())
    assert delta < 5, (
        f"parsed timestamp should equal current instant within 5s; "
        f"got delta={delta}s. If this is large, naive is being stamped "
        f"with tz=utc rather than converted from local."
    )


def test_aware_utc_passes_through_unchanged():
    aware = datetime(2026, 4, 17, 20, 0, 0, tzinfo=timezone.utc)
    parsed = parse_observation_ts(aware.isoformat())
    assert parsed == aware


def test_trailing_z_form_is_accepted():
    """audit.py writes "...Z" form; we must accept it."""
    parsed = parse_observation_ts("2026-04-17T20:00:00Z")
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)
    assert parsed.year == 2026 and parsed.hour == 20


def test_aware_non_utc_is_converted_to_utc():
    # A naive-local pytest fixture can't easily force a specific offset,
    # so use an explicit offset instead.
    aware_minus7 = datetime(
        2026, 4, 17, 13, 0, 0, tzinfo=timezone(timedelta(hours=-7))
    )
    parsed = parse_observation_ts(aware_minus7.isoformat())
    assert parsed.utcoffset() == timedelta(0)
    assert parsed.hour == 20  # 13:00 PHX = 20:00 UTC


def test_empty_string_raises():
    with pytest.raises(ValueError):
        parse_observation_ts("")


def test_garbage_raises():
    with pytest.raises(ValueError):
        parse_observation_ts("not-a-date")


def test_two_naive_local_strings_preserve_relative_order():
    """Parsing must preserve the relative ordering of two consecutive
    naive-local timestamps — otherwise 'most recent signal' calculations
    flip.
    """
    t1 = datetime.now().replace(microsecond=0)
    t2 = t1 + timedelta(seconds=30)
    p1 = parse_observation_ts(t1.isoformat())
    p2 = parse_observation_ts(t2.isoformat())
    assert p2 > p1
