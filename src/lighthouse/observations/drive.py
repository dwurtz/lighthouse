"""Google Drive signal collector using the gws CLI tool."""

from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime, timedelta, timezone

from lighthouse.observations.types import Observation

log = logging.getLogger(__name__)

MIME_LABELS = {
    "application/vnd.google-apps.document": "Google Doc",
    "application/vnd.google-apps.spreadsheet": "Google Sheet",
    "application/vnd.google-apps.presentation": "Google Slides",
    "application/vnd.google-apps.form": "Google Form",
}


def _readable_type(mime_type: str, filename: str) -> str:
    """Map a MIME type to a human-readable label."""
    if mime_type in MIME_LABELS:
        return MIME_LABELS[mime_type]
    # Fall back to file extension
    if "." in filename:
        return filename.rsplit(".", 1)[-1].upper()
    return "File"


def collect_recent_drive_activity(since_hours: int = 24) -> list[Observation]:
    """Collect recently created or modified Google Drive files using gws CLI."""
    signals: list[Observation] = []

    now = datetime.now(timezone.utc)
    created_since = (now - timedelta(hours=since_hours)).strftime("%Y-%m-%dT%H:%M:%S")
    modified_since = (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S")

    # --- Files created recently ---
    try:
        result = subprocess.run(
            [
                "gws", "drive", "files", "list",
                "--params", json.dumps({
                    "q": f'createdTime > "{created_since}"',
                    "fields": "files(id,name,mimeType,createdTime,modifiedTime)",
                    "pageSize": 10,
                    "orderBy": "createdTime desc",
                }),
                "--format", "json",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )

        if result.returncode != 0:
            log.warning("gws drive list (created) failed: %s", result.stderr[:200])
        else:
            data = json.loads(result.stdout)
            for f in data.get("files", []):
                file_id = f.get("id")
                name = f.get("name", "Untitled")
                mime = f.get("mimeType", "")
                if not file_id:
                    continue
                readable = _readable_type(mime, name)
                signals.append(Observation(
                    source="drive",
                    sender=readable,
                    text=f"Created: {name}",
                    timestamp=datetime.now(),
                    id_key=f"drive-{file_id}-created",
                ))

    except subprocess.TimeoutExpired:
        log.warning("gws drive list (created) timed out")
    except json.JSONDecodeError:
        log.warning("gws drive list (created) returned invalid JSON")
    except FileNotFoundError:
        log.warning("gws CLI not found on PATH")
    except Exception:
        log.exception("Drive collector error (created)")

    # --- Files modified recently (by me) ---
    try:
        result = subprocess.run(
            [
                "gws", "drive", "files", "list",
                "--params", json.dumps({
                    "q": f'modifiedTime > "{modified_since}" and "me" in owners',
                    "fields": "files(id,name,mimeType,modifiedTime)",
                    "pageSize": 10,
                    "orderBy": "modifiedTime desc",
                }),
                "--format", "json",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )

        if result.returncode != 0:
            log.warning("gws drive list (modified) failed: %s", result.stderr[:200])
        else:
            data = json.loads(result.stdout)
            for f in data.get("files", []):
                file_id = f.get("id")
                name = f.get("name", "Untitled")
                mime = f.get("mimeType", "")
                if not file_id:
                    continue
                readable = _readable_type(mime, name)
                signals.append(Observation(
                    source="drive",
                    sender=readable,
                    text=f"Modified: {name}",
                    timestamp=datetime.now(),
                    id_key=f"drive-{file_id}-modified",
                ))

    except subprocess.TimeoutExpired:
        log.warning("gws drive list (modified) timed out")
    except json.JSONDecodeError:
        log.warning("gws drive list (modified) returned invalid JSON")
    except FileNotFoundError:
        log.warning("gws CLI not found on PATH")
    except Exception:
        log.exception("Drive collector error (modified)")

    return signals
