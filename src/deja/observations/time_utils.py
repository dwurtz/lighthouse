"""Parse observation / audit timestamps into a consistent aware-UTC datetime.

Collectors write naive-local timestamps (``datetime.now()``,
``datetime.strptime(...)`` — no tzinfo). The audit log writes aware
UTC. Downstream code compares these against ``datetime.now(timezone.utc)``.
Mixing naive-local with aware-UTC is a class of bug this helper exists
to prevent:

  ts = datetime.fromisoformat(obs["timestamp"])
  if ts.tzinfo is None:
      ts = ts.replace(tzinfo=timezone.utc)   # WRONG — declares
                                              # local wall clock to
                                              # be UTC; silently
                                              # shifts age by
                                              # UTC_offset hours.

Use ``parse_observation_ts`` instead. It treats naive timestamps as
LOCAL (matching the collector convention) and converts to aware UTC,
so the resulting datetime can be compared against
``datetime.now(timezone.utc)`` correctly on any machine.

One-sentence contract: for any observation timestamp produced by any
Deja collector, ``parse_observation_ts`` returns an aware-UTC
datetime representing the same real-world instant.
"""

from __future__ import annotations

from datetime import datetime, timezone


def parse_observation_ts(ts_str: str) -> datetime:
    """Parse a timestamp string → aware UTC datetime.

    Accepts:
      * ISO 8601 naive local, e.g. ``"2026-04-17T13:59:51.174889"`` —
        treated as local (collector convention), converted to UTC.
      * ISO 8601 aware UTC, e.g. ``"2026-04-17T20:59:51+00:00"`` or
        ``"...Z"`` — returned with tz preserved.
      * ISO 8601 aware non-UTC — converted to UTC.

    Raises ``ValueError`` if the input is not a parseable ISO 8601
    datetime. Callers that need to tolerate garbage should catch.
    """
    if not ts_str:
        raise ValueError("empty timestamp")
    # Accept the stringified-Z form that audit.py writes
    normalized = ts_str.replace("Z", "+00:00")
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        # Naive → treat as local (matches collector.py convention).
        # .astimezone() on naive presumes the system tz, which is
        # exactly what we want here.
        return dt.astimezone(timezone.utc)
    return dt.astimezone(timezone.utc)
