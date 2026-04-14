"""Live integration tests for the gws CLI delta-polling params.

These tests exercise the actual ``gws`` subprocess with the exact
param shapes our observers use. They catch the class of bug where:

  - The gws CLI changes how it marshals a param shape
  - Google renames/removes a field
  - Our params produce a silent 400/404 that makes polling return 0
    events forever without raising

Gated behind the ``live_gws`` marker — requires Google Workspace auth
(``gws`` must be authenticated for the test user). Run explicitly:

    pytest tests/test_observer_delta_live.py -m live_gws -v

Don't run these in CI. Run them:

  - After upgrading gws
  - After touching any of the three observers
  - When debugging a "no new events" complaint

History of failures these tests catch:

- 2026-04-12: ``historyTypes=["messageAdded"]`` (JSON array form)
  returned 400 "Invalid value at 'history_types'". Live test would
  have caught this before shipping.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.live_gws


def _run_gws(args: list[str], timeout: int = 30) -> dict:
    """Run a gws command and return the parsed JSON response.

    Returns the raw response dict. If the response has an ``error``
    key, the test will explicitly check for that — we don't raise
    here so tests can distinguish between "auth missing" and "params
    broken."
    """
    r = subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if r.returncode != 0:
        pytest.fail(
            f"gws command failed (rc={r.returncode}): {r.stderr[:500]}\n"
            f"stdout: {r.stdout[:500]}"
        )
    # gws prints a keyring warning line before the JSON; strip it
    text = r.stdout.strip()
    # Find the first { that starts the JSON body
    start = text.find("{")
    if start == -1:
        pytest.fail(f"gws returned no JSON object: {text[:500]}")
    try:
        return json.loads(text[start:])
    except json.JSONDecodeError as e:
        pytest.fail(f"gws JSON parse failed: {e}\nraw: {text[:500]}")


def _gws_available() -> bool:
    try:
        r = subprocess.run(
            ["gws", "--version"],
            capture_output=True,
            timeout=5,
        )
        return r.returncode == 0
    except Exception:
        return False


@pytest.fixture(autouse=True)
def _skip_if_no_gws():
    if not _gws_available():
        pytest.skip("gws CLI not available on PATH")


# ---------------------------------------------------------------------------
# Gmail — history.list with messageAdded
# ---------------------------------------------------------------------------


def test_gmail_history_list_params_are_accepted():
    """The exact params our EmailObserver uses don't return 400.

    Previously: ``historyTypes=["messageAdded"]`` (array) returned
    400 "Invalid value at 'history_types'" — the gws CLI expected a
    string. A refactor that reintroduces array form would silently
    break Gmail delta polling; this test fails loudly instead.
    """
    # Get a fresh valid historyId first — old cursors may have expired
    profile = _run_gws([
        "gws", "gmail", "users", "getProfile",
        "--params", json.dumps({"userId": "me"}),
    ])
    assert "historyId" in profile, f"getProfile missing historyId: {profile}"
    hid = str(profile["historyId"])

    # Now exercise history.list with the EXACT param shape EmailObserver uses
    resp = _run_gws([
        "gws", "gmail", "users", "history", "list",
        "--params", json.dumps({
            "userId": "me",
            "startHistoryId": hid,
            "historyTypes": "messageAdded",  # string, NOT array
        }),
    ])
    # Must not be an error response
    assert "error" not in resp, (
        f"gws gmail history list returned an error with our params: "
        f"{resp['error']}. This is the exact bug the 'historyTypes as "
        f"string' comment in email.py warns about. If 'error' mentions "
        f"'Invalid value at history_types', revert to string form."
    )
    # historyId must always come back (even when history list is empty)
    assert "historyId" in resp, f"history list response missing historyId: {resp}"


def test_gmail_history_types_array_form_IS_rejected():
    """Negative test: confirm gws still rejects the array form.

    If this test starts passing, the gws CLI has loosened its marshalling
    and we could switch to the more natural array form. Until then,
    the string form is required.
    """
    profile = _run_gws([
        "gws", "gmail", "users", "getProfile",
        "--params", json.dumps({"userId": "me"}),
    ])
    hid = str(profile["historyId"])

    # Intentionally use the WRONG shape to verify the CLI still rejects it
    r = subprocess.run(
        [
            "gws", "gmail", "users", "history", "list",
            "--params", json.dumps({
                "userId": "me",
                "startHistoryId": hid,
                "historyTypes": ["messageAdded"],  # array — known-broken
            }),
        ],
        capture_output=True, text=True, timeout=30,
    )
    text = r.stdout + r.stderr
    # Should see a 400 or Invalid-value error somewhere in the output
    assert "400" in text or "Invalid value" in text or "history_types" in text.lower(), (
        "gws may have started accepting array-form historyTypes. "
        "If this test fails, we can simplify email.py to use the "
        "array form (more natural). Verify by running the observer "
        "with array-form params and checking it actually returns "
        "messages, not just stops erroring."
    )


# ---------------------------------------------------------------------------
# Calendar — events.list with syncToken
# ---------------------------------------------------------------------------


def test_calendar_events_list_initial_sync_works():
    """First-call shape (with timeMin) returns a nextSyncToken.

    This is what CalendarObserver does on first run to bootstrap.
    """
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    params = {
        "calendarId": "primary",
        "timeMin": (now - timedelta(hours=1)).isoformat(),
        "timeMax": (now + timedelta(hours=2)).isoformat(),
        "singleEvents": True,
    }
    resp = _run_gws([
        "gws", "calendar", "events", "list",
        "--params", json.dumps(params),
    ])
    assert "error" not in resp, f"calendar initial sync errored: {resp.get('error')}"
    # Either a nextSyncToken (finished) or a nextPageToken (more pages)
    assert "nextSyncToken" in resp or "nextPageToken" in resp or "items" in resp, (
        f"calendar events.list returned unexpected shape: {list(resp.keys())}"
    )


# ---------------------------------------------------------------------------
# Drive — changes.list with pageToken
# ---------------------------------------------------------------------------


def test_drive_get_start_page_token_works():
    """Bootstrap call returns a startPageToken.

    DriveObserver uses this on first run; a broken startPageToken
    means no delta polling possible.
    """
    resp = _run_gws([
        "gws", "drive", "changes", "getStartPageToken",
        "--params", "{}",
    ])
    assert "error" not in resp, f"drive getStartPageToken errored: {resp.get('error')}"
    assert "startPageToken" in resp, (
        f"drive getStartPageToken missing token: {resp}"
    )


def test_drive_changes_list_params_are_accepted():
    """The exact params DriveObserver uses don't return 400."""
    # Bootstrap a fresh token first
    boot = _run_gws([
        "gws", "drive", "changes", "getStartPageToken",
        "--params", "{}",
    ])
    token = boot["startPageToken"]

    # Now exercise changes.list with our shape
    resp = _run_gws([
        "gws", "drive", "changes", "list",
        "--params", json.dumps({
            "pageToken": token,
            "includeRemoved": True,
        }),
    ])
    assert "error" not in resp, (
        f"drive changes.list errored with our params: {resp.get('error')}"
    )
    # Either changes array + newStartPageToken, or nextPageToken
    assert (
        "newStartPageToken" in resp
        or "nextPageToken" in resp
        or "changes" in resp
    ), f"drive changes.list returned unexpected shape: {list(resp.keys())}"
