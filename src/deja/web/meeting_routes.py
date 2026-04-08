"""Meeting recording endpoints.

POST /api/meeting/start   — begin meeting recording session
POST /api/meeting/stop    — end meeting, trigger transcription + wiki
POST /api/meeting/unlink  — disconnect from calendar event
GET  /api/meeting/status  — current meeting recording state
GET  /api/meeting/prompt  — check if a recordable meeting is available
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone

from fastapi import APIRouter

from deja.meeting_transcribe import (
    MEETING_AUDIO_DIR,
    process_completed_meeting,
    transcribe_meeting_rolling,
)

log = logging.getLogger("deja.meeting")

router = APIRouter()

_meeting_state: dict = {
    "recording": False,
    "session_id": None,
    "metadata": None,
    "started_at": None,
    "transcripts": [],
    "chunks_transcribed": 0,
    "rolling_task": None,
}


@router.get("/api/meeting/prompt")
def meeting_prompt() -> dict:
    """Check if a recordable meeting is available."""
    from deja.meeting_coordinator import MEETING_PROMPT_PATH

    try:
        if MEETING_PROMPT_PATH.exists():
            data = json.loads(MEETING_PROMPT_PATH.read_text())
            return {"available": True, **data}
    except (json.JSONDecodeError, OSError):
        pass
    return {"available": False}


@router.post("/api/meeting/start")
async def meeting_start(body: dict) -> dict:
    """Begin a recording session.

    Body: { event_id?, title, attendees: [{name, email}] }
    """
    if _meeting_state["recording"]:
        return {
            "recording": True,
            "reason": "already recording",
            "session_id": _meeting_state["session_id"],
        }

    session_id = f"meeting-{int(time.time())}"
    session_dir = MEETING_AUDIO_DIR / session_id
    session_dir.mkdir(parents=True, exist_ok=True)

    started_at = datetime.now(timezone.utc).isoformat()

    metadata = {
        "event_id": body.get("event_id", ""),
        "title": body.get("title", "Meeting"),
        "attendees": body.get("attendees", []),
        "started_at": started_at,
    }

    (session_dir / "metadata.json").write_text(json.dumps(metadata, indent=2))

    _meeting_state["recording"] = True
    _meeting_state["session_id"] = session_id
    _meeting_state["metadata"] = metadata
    _meeting_state["started_at"] = started_at
    _meeting_state["transcripts"] = []
    _meeting_state["chunks_transcribed"] = 0

    _meeting_state["rolling_task"] = asyncio.create_task(
        transcribe_meeting_rolling(session_dir, _meeting_state)
    )

    from deja.meeting_coordinator import clear_meeting_prompt

    clear_meeting_prompt()

    from deja.meeting_coordinator import write_meeting_state

    write_meeting_state(
        {
            "recording": True,
            "session_id": session_id,
            "title": metadata["title"],
            "started_at": started_at,
        }
    )

    log.info(
        "Meeting recording started: %s (session %s)",
        metadata["title"],
        session_id,
    )

    return {
        "recording": True,
        "session_id": session_id,
        "session_dir": str(session_dir),
        "started_at": started_at,
    }


@router.post("/api/meeting/unlink")
async def meeting_unlink() -> dict:
    """Disconnect from calendar event but keep recording."""
    if _meeting_state["metadata"]:
        _meeting_state["metadata"]["event_id"] = ""
        _meeting_state["metadata"]["title"] = ""
        _meeting_state["metadata"]["attendees"] = []
    return {"unlinked": True}


@router.post("/api/meeting/stop")
async def meeting_stop(body: dict = None) -> dict:
    """End the recording and create a wiki event page."""
    if not _meeting_state["recording"]:
        return {"recording": False, "reason": "no active session"}

    session_id = _meeting_state["session_id"]
    session_dir = MEETING_AUDIO_DIR / session_id

    user_notes = ""
    if body and isinstance(body, dict):
        user_notes = body.get("notes", "")
    if user_notes and _meeting_state.get("metadata"):
        _meeting_state["metadata"]["user_notes"] = user_notes

    log.info("Recording stopped: %s", session_id)

    task = _meeting_state.get("rolling_task")
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    result = await process_completed_meeting(session_dir, _meeting_state)

    _meeting_state["recording"] = False
    _meeting_state["session_id"] = None
    _meeting_state["metadata"] = None
    _meeting_state["started_at"] = None
    _meeting_state["transcripts"] = []
    _meeting_state["chunks_transcribed"] = 0
    _meeting_state["rolling_task"] = None

    from deja.meeting_coordinator import clear_meeting_state

    clear_meeting_state()

    return {
        "recording": False,
        "session_id": session_id,
        **result,
    }


@router.get("/api/meeting/status")
def meeting_status() -> dict:
    """Current meeting recording state."""
    if not _meeting_state["recording"]:
        return {"recording": False}

    elapsed = 0
    if _meeting_state["started_at"]:
        try:
            start = datetime.fromisoformat(_meeting_state["started_at"])
            elapsed = int((datetime.now(timezone.utc) - start).total_seconds())
        except Exception:
            pass

    return {
        "recording": True,
        "session_id": _meeting_state["session_id"],
        "title": (_meeting_state.get("metadata") or {}).get("title", ""),
        "started_at": _meeting_state["started_at"],
        "elapsed_sec": elapsed,
        "chunks_transcribed": _meeting_state["chunks_transcribed"],
    }
