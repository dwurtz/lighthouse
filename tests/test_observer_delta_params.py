"""Regression tests for the delta-polling call shapes used by the
Gmail / Calendar / Drive observers.

Gmail now uses direct ``googleapiclient`` calls (see
``deja.google_api``); Calendar + Drive still use the gws CLI. Every
time we touch one of these observers it's tempting to pass the wrong
param form and get a silent 400 in production. These tests pin the
exact shapes that were empirically verified to work.

If any of these tests fail after a refactor, the observer is about to
silently stop ingesting events. The log will show 400 / 404 errors
but the agent loop keeps running.

History of failures these tests prevent:

- 2026-04-12: ``historyTypes=["messageAdded"]`` sent as a JSON array
  to gws returned 400 "Invalid value at 'history_types'". gws wanted
  the string form. After migration to direct googleapiclient calls
  the native array form works again — but the ``history.list`` call
  path must still exist (not a revert to ``messages.list`` scanning).
"""

from __future__ import annotations

import json
from pathlib import Path


OBSERVATIONS_DIR = Path(__file__).resolve().parent.parent / "src" / "deja" / "observations"


def _read(name: str) -> str:
    return (OBSERVATIONS_DIR / name).read_text(encoding="utf-8")


def test_gmail_history_types_uses_messageAdded_filter():
    """The history.list call must filter on ``messageAdded``.

    Direct googleapiclient accepts either the native array form
    (``["messageAdded"]``) or the repeated-query-param form. Both
    reduce to the same HTTP request. What we MUST keep is the filter
    itself — without it, every cycle pulls label changes, drafts,
    and deletions too, which floods the pipeline with noise.
    """
    src = _read("email.py")
    assert "messageAdded" in src, (
        "EmailObserver must filter history.list on messageAdded. "
        "Without the filter the observer ingests label changes, draft "
        "updates, and deletions — every cycle balloons to hundreds of "
        "no-op records."
    )


def test_gmail_delta_uses_history_list_not_messages_list():
    """The delta poller must use history.list (cheap, cursor-based),
    not messages.list with a time-window query (expensive, full scan).

    A refactor that reverts to ``messages.list q=... newer_than:5m``
    would work functionally but waste API quota and miss the whole
    point of the delta switch. Pin the cheaper path.
    """
    src = _read("email.py")
    # Direct googleapiclient call: svc.users().history().list(...)
    assert ".users().history().list(" in src or "users.history.list" in src, (
        "EmailObserver should call users.history.list for delta "
        "polling. If you're reading this after a revert to "
        "messages.list, that's a regression — the history API is "
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
