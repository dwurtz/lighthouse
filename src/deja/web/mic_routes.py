"""Microphone recording endpoints.

POST /api/mic/start  — begin push-to-record session (ffmpeg + TCC)
POST /api/mic/stop   — end session, transcribe via Gemini native audio
GET  /api/mic/status  — {recording, started_at}
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import signal as _signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter

from deja.config import DEJA_HOME
from deja.web.helpers import (
    OBSERVATIONS_LOG,
    load_conversation,
    save_conversation,
)

log = logging.getLogger("deja.mic")

router = APIRouter()


def _get_groq_key() -> str:
    """Read Groq API key from ~/.deja/config.json."""
    config_path = DEJA_HOME / "config.json"
    if config_path.exists():
        import json as _json
        config = _json.loads(config_path.read_text())
        key = config.get("groq_api_key", "")
        if key:
            return key
    raise RuntimeError("Groq API key not configured in ~/.deja/config.json")


async def _transcribe_groq(wav_path: Path) -> str:
    """Transcribe a WAV file using Groq's Whisper API."""
    import httpx

    key = _get_groq_key()
    audio_bytes = wav_path.read_bytes()

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {key}"},
            files={"file": ("audio.wav", audio_bytes, "audio/wav")},
            data={"model": "whisper-large-v3"},
        )
        resp.raise_for_status()
        result = resp.json()

    transcript = (result.get("text") or "").strip()
    log.info("groq whisper: %d bytes → %r", len(audio_bytes), transcript[:200])
    return transcript

AUDIO_DIR = DEJA_HOME / "audio"
MIC_AUTO_STOP_SEC = 300  # 5 minutes

_mic_state: dict = {
    "process": None,
    "wav_path": None,
    "started_at": None,
    "auto_stop_task": None,
}


async def _auto_stop_after(delay: float) -> None:
    """Safety net: kill the mic session after *delay* seconds if still running."""
    try:
        await asyncio.sleep(delay)
    except asyncio.CancelledError:
        return
    if _mic_state["process"] is not None:
        await _mic_stop_inner(reason="auto-stop (safety timeout)")


async def _mic_stop_inner(reason: str = "manual") -> dict:
    """Stop ffmpeg, transcribe, emit signal. Idempotent."""
    proc: subprocess.Popen | None = _mic_state.get("process")
    wav_path: Path | None = _mic_state.get("wav_path")
    started_at: str | None = _mic_state.get("started_at")

    task = _mic_state.get("auto_stop_task")
    if task is not None and not task.done():
        task.cancel()
    _mic_state["auto_stop_task"] = None

    if proc is None or wav_path is None:
        _mic_state["process"] = None
        _mic_state["wav_path"] = None
        _mic_state["started_at"] = None
        return {"recording": False, "reason": "no active session"}

    try:
        proc.send_signal(_signal.SIGINT)
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2)
    except Exception:
        pass

    _mic_state["process"] = None
    _mic_state["wav_path"] = None
    _mic_state["started_at"] = None

    if not wav_path.exists() or wav_path.stat().st_size < 4096:
        size = wav_path.stat().st_size if wav_path.exists() else 0
        log.warning("mic_stop: wav too small (%d bytes) at %s", size, wav_path)
        try:
            wav_path.unlink(missing_ok=True)
        except Exception:
            pass
        return {"recording": False, "reason": f"no audio captured ({reason})"}

    log.info("mic_stop: wav=%s size=%d bytes", wav_path, wav_path.stat().st_size)

    # Transcribe via Groq Whisper (fast, dedicated speech-to-text).
    transcript = ""
    transcribe_error = None
    try:
        transcript = await _transcribe_groq(wav_path)
    except Exception as e:
        transcribe_error = str(e)
        log.exception("mic_stop: transcription failed")

    if transcript:
        try:
            wav_path.unlink(missing_ok=True)
        except Exception:
            pass
    else:
        log.warning(
            "mic_stop: empty transcript, keeping wav for debug at %s", wav_path
        )

    transcript = (transcript or "").strip()
    if not transcript:
        return {
            "recording": False,
            "reason": reason,
            "transcript": "",
            "error": transcribe_error or "no speech detected",
        }

    from deja.identity import load_user

    user = load_user()

    ts = datetime.now(timezone.utc).isoformat()
    id_key = (
        "mic-"
        + hashlib.md5(f"{ts}-{transcript[:200]}".encode()).hexdigest()[:16]
    )

    # 1. Append to conversation.json
    messages = load_conversation()
    messages.append(
        {
            "role": "user",
            "content": transcript,
            "timestamp": ts,
            "source": "voice",
        }
    )
    save_conversation(messages)

    # 2. Persist as a chat-equivalent observation
    try:
        OBSERVATIONS_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(OBSERVATIONS_LOG, "a") as f:
            f.write(
                json.dumps(
                    {
                        "source": "chat",
                        "sender": "You",
                        "text": f"[spoken] {transcript[:2000]}",
                        "timestamp": ts,
                        "id_key": id_key,
                    }
                )
                + "\n"
            )
    except Exception:
        pass

    # 3. Human-readable log in the wiki
    try:
        from deja.activity_log import append_log_entry

        preview = " ".join(transcript.split())[:120]
        append_log_entry("chat", f"{user.first_name} (spoken): {preview}")
    except Exception:
        pass

    return {
        "recording": False,
        "reason": reason,
        "started_at": started_at,
        "transcript": transcript,
    }


@router.post("/api/mic/start")
async def mic_start() -> dict:
    if _mic_state["process"] is not None:
        return {
            "recording": True,
            "reason": "already recording",
            "started_at": _mic_state["started_at"],
        }

    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    wav_path = AUDIO_DIR / f"session-{int(time.time())}.wav"

    cmd = [
        "ffmpeg",
        "-f",
        "avfoundation",
        "-i",
        ":0",
        "-ar",
        "16000",
        "-ac",
        "1",
        "-y",
        "-loglevel",
        "error",
        str(wav_path),
    ]
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError:
        return {"recording": False, "error": "ffmpeg not installed"}
    except Exception as e:
        return {"recording": False, "error": f"ffmpeg spawn failed: {e}"}

    started_at = datetime.now(timezone.utc).isoformat()
    _mic_state["process"] = proc
    _mic_state["wav_path"] = wav_path
    _mic_state["started_at"] = started_at

    _mic_state["auto_stop_task"] = asyncio.create_task(
        _auto_stop_after(MIC_AUTO_STOP_SEC)
    )

    return {
        "recording": True,
        "started_at": started_at,
        "auto_stop_sec": MIC_AUTO_STOP_SEC,
    }


@router.post("/api/mic/stop")
async def mic_stop() -> dict:
    return await _mic_stop_inner(reason="manual")


@router.get("/api/mic/status")
def mic_status() -> dict:
    return {
        "recording": _mic_state["process"] is not None,
        "started_at": _mic_state.get("started_at"),
    }
