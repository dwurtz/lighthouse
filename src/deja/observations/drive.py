"""Google Drive signal collector using the gws CLI tool.

Uses Drive's changes feed (``changes.list`` with a ``pageToken`` cursor)
so each cycle only pulls files that were created, modified, or removed
since the last poll.

Cursor persistence: ``~/.deja/drive_page_token.txt``. On first run we
bootstrap via ``changes.getStartPageToken`` which returns the current
token — we START there and do NOT backfill. Historical Drive activity
is onboarding's problem.

No silent fallback to snapshot polling — on failure we log.error and
return [].
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from deja.config import DEJA_HOME
from deja.observations.base import BaseObserver
from deja.observations.types import Observation

log = logging.getLogger(__name__)

MIME_LABELS = {
    "application/vnd.google-apps.document": "Google Doc",
    "application/vnd.google-apps.spreadsheet": "Google Sheet",
    "application/vnd.google-apps.presentation": "Google Slides",
    "application/vnd.google-apps.form": "Google Form",
}

_CURSOR_PATH = DEJA_HOME / "drive_page_token.txt"


def _readable_type(mime_type: str, filename: str) -> str:
    """Map a MIME type to a human-readable label."""
    if mime_type in MIME_LABELS:
        return MIME_LABELS[mime_type]
    if "." in filename:
        return filename.rsplit(".", 1)[-1].upper()
    return "File"


# ---------------------------------------------------------------------------
# Cursor helpers
# ---------------------------------------------------------------------------


def _read_cursor() -> str | None:
    try:
        if not _CURSOR_PATH.exists():
            return None
        raw = _CURSOR_PATH.read_text().strip()
        return raw or None
    except OSError:
        log.warning("Drive page token unreadable; will bootstrap")
        return None


def _write_cursor_atomic(token: str) -> None:
    try:
        DEJA_HOME.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".drive_token.", dir=str(DEJA_HOME))
        try:
            with os.fdopen(fd, "w") as f:
                f.write(str(token))
            os.replace(tmp, _CURSOR_PATH)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception:
        log.exception("Failed to persist Drive page token")


def _bootstrap_cursor() -> str | None:
    """Bootstrap the page token via changes.getStartPageToken."""
    try:
        result = subprocess.run(
            [
                "gws", "drive", "changes", "getStartPageToken",
                "--params", json.dumps({}),
                "--format", "json",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            log.error("gws drive getStartPageToken failed: %s", result.stderr[:200])
            return None
        data = json.loads(result.stdout)
        token = str(data.get("startPageToken") or "")
        if not token:
            log.error("gws drive getStartPageToken returned no token")
            return None
        _write_cursor_atomic(token)
        log.info("Drive page token bootstrapped at %s", token)
        return token
    except subprocess.TimeoutExpired:
        log.error("gws drive getStartPageToken timed out")
        return None
    except (json.JSONDecodeError, FileNotFoundError) as e:
        log.error("gws drive getStartPageToken failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Changes delta fetch
# ---------------------------------------------------------------------------


def _list_changes(start_token: str) -> tuple[list[dict], str | None]:
    """Page through changes.list since ``start_token``.

    Returns (changes, new_start_page_token). The returned token is what
    to persist for the NEXT poll (Drive API returns ``newStartPageToken``
    on the final page).
    """
    changes: list[dict] = []
    page_token: str | None = start_token
    new_start: str | None = None

    for _ in range(20):
        params: dict = {
            "pageToken": page_token,
            "includeRemoved": True,
            "pageSize": 100,
            "fields": (
                "nextPageToken,newStartPageToken,"
                "changes(fileId,removed,time,file(id,name,mimeType,modifiedTime,createdTime))"
            ),
        }
        try:
            result = subprocess.run(
                [
                    "gws", "drive", "changes", "list",
                    "--params", json.dumps(params),
                    "--format", "json",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            log.error("gws drive changes list timed out")
            return [], None
        except FileNotFoundError:
            log.error("gws CLI not found on PATH")
            return [], None

        if result.returncode != 0:
            stderr = result.stderr or ""
            # Invalid token → re-bootstrap
            if "invalid" in stderr.lower() and "token" in stderr.lower():
                log.error("Drive page token invalid; re-bootstrapping")
                new = _bootstrap_cursor()
                return [], new
            log.error("gws drive changes list failed: %s", stderr[:200])
            return [], None

        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            log.error("gws drive changes list returned invalid JSON")
            return [], None

        changes.extend(data.get("changes", []) or [])
        next_page = data.get("nextPageToken")
        if next_page:
            page_token = next_page
            continue
        new_start = data.get("newStartPageToken")
        break

    return changes, new_start


# ---------------------------------------------------------------------------
# Observer
# ---------------------------------------------------------------------------


class DriveObserver(BaseObserver):
    """Delta-based Drive collector via changes.list."""

    def __init__(self, since_hours: int = 24) -> None:
        # Retained for API compat; unused in delta mode.
        self.since_hours = since_hours

    @property
    def name(self) -> str:
        return "Drive"

    def collect(self) -> list[Observation]:
        cursor = _read_cursor()
        if cursor is None:
            cursor = _bootstrap_cursor()
            if cursor is None:
                log.error("Drive collector: no cursor available, skipping cycle")
                return []
            # Fresh bootstrap — nothing to emit this cycle.
            return []

        changes, new_token = _list_changes(cursor)
        if new_token:
            _write_cursor_atomic(new_token)

        observations: list[Observation] = []
        for change in changes:
            file_id = change.get("fileId")
            if not file_id:
                continue
            removed = bool(change.get("removed"))
            change_time = change.get("time") or ""

            if removed:
                observations.append(Observation(
                    source="drive",
                    sender="File",
                    text=f"Removed: {file_id}",
                    timestamp=datetime.now(),
                    id_key=f"drive-{file_id}-removed-{change_time}",
                ))
                continue

            f = change.get("file") or {}
            name = f.get("name", "Untitled")
            mime = f.get("mimeType", "")
            readable = _readable_type(mime, name)
            created = f.get("createdTime", "")
            modified = f.get("modifiedTime", "")

            # Distinguish create vs modify by comparing the two timestamps.
            # Drive sets them identically on creation.
            if created and created == modified:
                verb = "Created"
                kind = "created"
            else:
                verb = "Modified"
                kind = "modified"

            observations.append(Observation(
                source="drive",
                sender=readable,
                text=f"{verb}: {name}",
                timestamp=datetime.now(),
                # Include change_time in id_key so repeated edits to the same
                # file produce distinct observations (Drive re-emits the same
                # fileId every time it's touched).
                id_key=f"drive-{file_id}-{kind}-{change_time or modified or ''}",
            ))

        return observations


# ---------------------------------------------------------------------------
# Back-compat helper (no longer used by the observer; retained for callers)
# ---------------------------------------------------------------------------


def collect_recent_drive_activity(since_hours: int = 24) -> list[Observation]:
    """Deprecated: use ``DriveObserver().collect()``. Delegates to delta path."""
    return DriveObserver(since_hours=since_hours).collect()
