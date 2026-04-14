"""Regression tests for the gws CLI parameter shapes used by the
Gmail / Calendar / Drive delta observers.

The gws CLI marshals certain params differently from the raw Google
API specs. Every time we touch one of these observers it's tempting
to pass an array where the CLI expects a string (or vice versa) and
get a silent 400 error in production. These tests pin the exact
shapes that were empirically verified to work.

If any of these tests fail after a refactor, the observer is about
to silently stop ingesting events. The log will show 400 / 404 errors
from the gws subprocess but the agent loop keeps running.

History of failures these tests prevent:

- 2026-04-12: ``historyTypes=["messageAdded"]`` sent as a JSON array
  returned 400 "Invalid value at 'history_types'". The gws CLI wants
  the string form ``"messageAdded"``. Gmail delta polling silently
  failed for multiple hours before the 502 log pattern exposed it.
"""

from __future__ import annotations

import json
from pathlib import Path


OBSERVATIONS_DIR = Path(__file__).resolve().parent.parent / "src" / "deja" / "observations"


def _read(name: str) -> str:
    return (OBSERVATIONS_DIR / name).read_text(encoding="utf-8")


def test_gmail_history_types_is_string_not_array():
    """gws CLI expects ``historyTypes`` as a string, not a JSON array.

    The raw Gmail API accepts an array; gws marshals it differently.
    Passing an array produces a 400 with a cryptic "Invalid value at
    'history_types'" error and the observer silently returns no
    messages.
    """
    src = _read("email.py")
    # The correct shape — string, not array
    assert '"historyTypes": "messageAdded"' in src, (
        "gws gmail users history list params must use historyTypes as a "
        "STRING ('messageAdded'), not a JSON array. The array form 400s "
        "with 'Invalid value at history_types' and silently breaks the "
        "Gmail delta poller."
    )
    # And the array form must NOT be present
    assert '"historyTypes": ["messageAdded"]' not in src, (
        "Found array-form historyTypes in email.py — this WILL 400 at "
        "runtime. Change to the string form."
    )


def test_gmail_delta_uses_history_list_not_messages_list():
    """The delta poller must use history.list (cheap, cursor-based),
    not messages.list with a time-window query (expensive, full scan).

    A refactor that reverts to ``messages list --params '...q=in:sent
    newer_than:5m'`` would work functionally but waste API quota and
    miss the whole point of the delta switch. Pin the cheaper path.
    """
    src = _read("email.py")
    # The EmailObserver.collect path must call history list
    assert "gws gmail users history list" in src or "gws\", \"gmail\", \"users\", \"history\", \"list\"" in src or (
        '"users", "history", "list"' in src
    ), (
        "EmailObserver should call `gws gmail users history list` for "
        "delta polling. If you're reading this after a revert to "
        "`messages list`, that's a regression — the history API is "
        "cheaper and avoids missed messages on the boundary."
    )


def test_calendar_delta_uses_sync_token_not_time_window():
    """Calendar delta poller uses syncToken, not timeMin/timeMax scanning."""
    src = _read("calendar.py")
    assert "syncToken" in src, (
        "CalendarObserver should persist and reuse a syncToken for "
        "delta polling. Missing syncToken in calendar.py suggests a "
        "revert to time-window polling."
    )


def test_drive_delta_uses_page_token_not_modified_time():
    """Drive delta poller uses pageToken + changes.list, not modifiedTime filter."""
    src = _read("drive.py")
    assert "changes" in src and "pageToken" in src, (
        "DriveObserver should use `changes list` with a pageToken "
        "cursor for delta polling, not `files list` with modifiedTime "
        "filtering."
    )


def test_cursor_paths_are_in_deja_home():
    """Cursor files live under ~/.deja/ — not in the wiki, not in /tmp."""
    email_src = _read("email.py")
    calendar_src = _read("calendar.py")
    drive_src = _read("drive.py")

    # Each should reference DEJA_HOME for its cursor file
    for name, src in [
        ("email", email_src),
        ("calendar", calendar_src),
        ("drive", drive_src),
    ]:
        assert "DEJA_HOME" in src, (
            f"{name}.py should persist its delta cursor under "
            f"DEJA_HOME. If the cursor lands somewhere else, it gets "
            f"wiped on clean installs or overwritten by other users."
        )
