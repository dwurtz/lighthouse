"""Microphone recording endpoints.

POST /api/mic/start  — begin push-to-record session (ffmpeg + TCC)
POST /api/mic/stop   — end session, transcribe via Gemini native audio
GET  /api/mic/status  — {recording, started_at}
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter

from deja.config import DEJA_HOME
from deja.web.helpers import (
    load_conversation,
    save_conversation,
)

log = logging.getLogger("deja.mic")

router = APIRouter()


async def _transcribe_groq(wav_path: Path) -> str:
    """Transcribe a WAV file via the Deja API proxy (Groq Whisper)."""
    import httpx
    from deja.llm_client import DEJA_API_URL
    from deja.auth import get_auth_token

    audio_bytes = wav_path.read_bytes()
    token = get_auth_token()
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{DEJA_API_URL}/v1/transcribe",
            headers=headers,
            files={"file": ("audio.wav", audio_bytes, "audio/wav")},
        )
        resp.raise_for_status()
        result = resp.json()

    transcript = (result.get("text") or "").strip()
    log.info("transcribe: %d bytes → %r", len(audio_bytes), transcript[:200])
    return transcript


# Prompt adapted from voquill/voquill scripts/prompts/polished.txt (AGPLv3).
# The load-bearing constraint is "Without changing word-choice" — polish is
# a grammar/formatting pass, not a paraphrase.
_POLISH_PROMPT = """Without changing word-choice, clean up the transcript.
Fix grammar, punctuation, and formatting.
Remove filler words, false starts, and repetitions. Keep the original meaning intact.
Break the response into paragraphs when appropriate.
Format spoken lists into bulleted or numbered lists.
Convert spoken symbols into their actual character equivalents (e.g. "hashtag" to "#", emojis, `foo.cpp`, etc).
When the speaker corrects themselves, only keep the corrected version.
Convert spoken dates, times, and numbers into their proper numerical forms.
Do NOT use em-dash symbols (—) in your response.
Respond with JSON only: { "result": "<cleaned transcript>" }

Transcript:
\"\"\"
%s
\"\"\"
"""


_POLISH_MODEL = "llama-3.1-8b-instant"


async def _polish_transcript(raw: str) -> str:
    """Clean up a raw voice transcript via Groq llama-3.1-8b-instant.

    Fixes grammar, punctuation, fillers, self-corrections, and spoken
    symbols without changing word choice. Hits Deja's /v1/chat proxy
    endpoint which routes to Groq's OpenAI-compatible chat completions
    API. Falls back to the raw transcript on any error so we never
    lose content.

    Why Groq 8B vs Gemini Flash-Lite: ~5× faster (800+ tok/s vs ~150
    tok/s) and ~3× cheaper per call. Polish is a trivial task — filler
    removal and grammar fixes don't need a frontier model. Quality
    ceiling saturates well below 8B params.
    """
    if not raw or len(raw.strip()) < 5:
        return raw

    try:
        import httpx
        from deja.llm_client import DEJA_API_URL
        from deja.auth import get_auth_token

        token = get_auth_token()
        headers = {"Authorization": f"Bearer {token}"} if token else {}

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{DEJA_API_URL}/v1/chat",
                headers=headers,
                json={
                    "model": _POLISH_MODEL,
                    "messages": [
                        {"role": "user", "content": _POLISH_PROMPT % raw},
                    ],
                    "temperature": 0.1,
                    "max_tokens": 2048,
                    "response_format": {"type": "json_object"},
                },
            )
            resp.raise_for_status()
            text = (resp.json().get("text") or "").strip()

        data = json.loads(text)
        cleaned = (data.get("result") or "").strip()
        if cleaned:
            log.info("polish: %d → %d chars (groq 8b)", len(raw), len(cleaned))
            return cleaned
        log.warning("polish: empty result — keeping raw")
    except Exception:
        log.exception("polish: failed — keeping raw transcript")

    return raw

AUDIO_DIR = DEJA_HOME / "audio"


def _find_recorder() -> str:
    """Find the DejaRecorder binary — bundled in app or in build output."""
    import sys

    candidates = []
    # Inside Deja.app bundle
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).parent / "DejaRecorder")
    # App bundle in /Applications
    candidates.append(Path("/Applications/Deja.app/Contents/MacOS/DejaRecorder"))
    # Development build output
    candidates.append(Path.home() / "projects" / "deja" / "build" / "Release" / "DejaRecorder")

    for p in candidates:
        if p.exists():
            return str(p)

    raise FileNotFoundError("DejaRecorder binary not found")


def _has_speech(wav_path: Path, threshold: float = 0.003) -> bool:
    """Check if a WAV file contains actual speech above a noise threshold.

    Reads the raw PCM data and checks RMS level. Returns False for
    silence/ambient noise to avoid Whisper hallucinations.
    """
    import struct
    import wave

    try:
        with wave.open(str(wav_path), "rb") as wf:
            frames = wf.readframes(wf.getnframes())
            sw = wf.getsampwidth()
            if len(frames) < 100:
                return False

            if sw == 2:
                # 16-bit PCM
                samples = struct.unpack(f"<{len(frames) // 2}h", frames)
                rms = (sum(s * s for s in samples) / len(samples)) ** 0.5
                level = rms / 32768.0
            elif sw == 4:
                # 32-bit float
                samples = struct.unpack(f"<{len(frames) // 4}f", frames)
                rms = (sum(s * s for s in samples) / len(samples)) ** 0.5
                level = rms  # already 0-1 range
            else:
                log.info("_has_speech: unexpected sample width %d — proceeding", sw)
                return True

            log.info("audio RMS level: %.6f (threshold: %.4f, sw=%d, frames=%d)",
                     level, threshold, sw, len(frames))
            return level > threshold
    except Exception as e:
        log.warning("_has_speech check failed: %s — proceeding with transcription", e)
        return True


def _find_mic_device() -> int:
    """Find the best microphone device index for ffmpeg avfoundation.

    Prefers external mics (AirPods, USB) over built-in, and skips
    virtual/loopback devices (Descript, BlackHole, Soundflower, etc.).
    Falls back to device 0 if nothing better is found.
    """
    import re

    try:
        result = subprocess.run(
            ["ffmpeg", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
            capture_output=True, text=True, timeout=5,
        )
        output = result.stderr  # ffmpeg writes device list to stderr
    except Exception:
        return 0

    # Parse audio device lines: [N] Device Name
    skip_words = {"loopback", "blackhole", "soundflower", "virtual", "descript"}
    devices: list[tuple[int, str]] = []
    in_audio = False
    for line in output.splitlines():
        if "audio devices" in line.lower():
            in_audio = True
            continue
        if in_audio:
            m = re.search(r"\[(\d+)\]\s+(.+)", line)
            if m:
                idx, name = int(m.group(1)), m.group(2).strip()
                if not any(w in name.lower() for w in skip_words):
                    devices.append((idx, name))
            elif "video devices" in line.lower():
                break

    if not devices:
        return 0

    # Prefer wired/USB mics (instant, no lag) over Bluetooth (has activation delay)
    wired_words = {"usb", "blue", "yeti", "rode", "scarlett", "focusrite"}
    for idx, name in devices:
        if any(w in name.lower() for w in wired_words):
            log.info("mic device: [%d] %s (wired external — preferred)", idx, name)
            return idx

    # Built-in MacBook mic — always reliable, no activation delay
    for idx, name in devices:
        if "macbook" in name.lower() or "built-in" in name.lower():
            log.info("mic device: [%d] %s (built-in)", idx, name)
            return idx

    # Last resort: first non-virtual device (may include Bluetooth)
    idx, name = devices[0]
    log.info("mic device: [%d] %s (first available)", idx, name)
    return idx
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
    """Stop recording via SIGTERM, transcribe, emit signal. Idempotent."""
    wav_path: Path | None = _mic_state.get("wav_path")
    started_at: str | None = _mic_state.get("started_at")
    proc: subprocess.Popen | None = _mic_state.get("process")

    task = _mic_state.get("auto_stop_task")
    if task is not None and not task.done():
        task.cancel()
    _mic_state["auto_stop_task"] = None

    if not _mic_state.get("recording") or wav_path is None or proc is None:
        _mic_state["recording"] = False
        _mic_state["process"] = None
        _mic_state["wav_path"] = None
        _mic_state["started_at"] = None
        return {"recording": False, "reason": "no active session"}

    # Ask DejaRecorder to stop: SIGTERM → signal handler flips
    # _micShouldStop → timer runs stop() → AVAudioFile flushes → exit(0).
    try:
        proc.terminate()
    except ProcessLookupError:
        pass  # already dead
    except Exception:
        log.exception("mic_stop: proc.terminate failed")

    # Wait up to 2s for clean exit; SIGKILL as a last resort.
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, lambda: proc.wait(timeout=2.0))
    except subprocess.TimeoutExpired:
        log.warning("mic_stop: recorder didn't exit after SIGTERM — SIGKILLing")
        try:
            proc.kill()
            await loop.run_in_executor(None, proc.wait)
        except Exception:
            log.exception("mic_stop: proc.kill failed")

    _mic_state["recording"] = False
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

    wav_size = wav_path.stat().st_size
    log.info("mic_stop: wav=%s size=%d bytes", wav_path, wav_size)

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

    # Filter known Whisper hallucinations from near-silent audio.
    # Surface as a structured failure so the UI can show a "couldn't
    # understand you — try again" toast instead of silently dropping.
    _HALLUCINATIONS = {
        "you", "thank you", "thanks", "thank you.", "thanks.",
        "thanks for watching", "thanks for watching.",
        "thank you for watching", "thank you for watching.",
        "bye", "bye.", "goodbye", "goodbye.",
        "you.", "the end", "the end.",
    }
    if transcript.lower().strip(".!? ") in _HALLUCINATIONS:
        raw = transcript
        log.info("mic_stop: filtered Whisper hallucination: %r", raw)
        # Activity log entry so the dropped transcription shows in feed
        try:
            from deja.activity_log import append_log_entry

            kb = wav_size // 1024
            append_log_entry(
                "mic",
                f"transcription dropped — Whisper hallucination: {raw!r} ({kb}KB audio)",
            )
        except Exception:
            log.debug("activity_log append failed for dropped transcription", exc_info=True)
        return {
            "ok": False,
            "recording": False,
            "reason": "transcription_dropped",
            "detail": "Couldn't understand audio — try speaking louder or closer to the mic.",
            "raw_transcript": raw,
            "wav_size_bytes": wav_size,
        }

    if not transcript:
        return {
            "ok": False,
            "recording": False,
            "reason": reason,
            "transcript": "",
            "error": transcribe_error or "no speech detected",
        }

    # Polish pass — fix grammar, remove fillers, convert spoken symbols
    # without changing word choice. Flash-Lite via the proxy, ~1s latency.
    transcript = await _polish_transcript(transcript)

    from deja.identity import load_user

    user = load_user()

    ts = datetime.now(timezone.utc).isoformat()

    # 1. Append to conversation.json (keeps the voice history visible)
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

    # 2. Route through the command classifier — every voice utterance
    #    becomes a classifier call. The classifier decides if it's a
    #    one-off action (calendar/email/task/notify), a goal/waiting-for
    #    item, an automation rule, or pure "context" (in which case the
    #    context dispatch appends to observations.jsonl + fires
    #    integrate, preserving the old "voice → observation" behavior
    #    for notes-style dictations). This is the ONE code path that
    #    can turn "put a test on my calendar" into a calendar_create.
    try:
        from deja.web.command_routes import (
            _classify,
            _dispatch_action,
            _dispatch_automation,
            _dispatch_context,
            _dispatch_goal,
            CommandResponse,
        )
    except Exception:
        log.exception("mic_stop: failed to import command_routes")
        raise

    try:
        parsed, cost, latency_ms = await _classify(transcript)
    except Exception as e:
        log.exception("mic_stop: classifier failed")
        return {
            "ok": False,
            "recording": False,
            "reason": "classifier_failed",
            "detail": f"Classifier failed: {e}",
            "transcript": transcript,
        }

    cmd_type = parsed.get("type")
    payload = parsed.get("payload") or {}
    confirmation = parsed.get("confirmation", "") or ""

    try:
        if cmd_type == "action":
            details = _dispatch_action(payload)
        elif cmd_type == "goal":
            details = _dispatch_goal(payload)
        elif cmd_type == "automation":
            details = _dispatch_automation(payload)
        elif cmd_type == "context":
            details = _dispatch_context(payload)
        else:
            return {
                "ok": False,
                "recording": False,
                "reason": "classifier_unknown_type",
                "detail": f"Unknown command type: {cmd_type!r}",
                "transcript": transcript,
            }
    except Exception as e:
        log.exception("mic_stop: dispatch failed for type=%s", cmd_type)
        return {
            "ok": False,
            "recording": False,
            "reason": "dispatch_failed",
            "detail": f"Dispatch failed: {e}",
            "transcript": transcript,
        }

    # Activity log — mirror command_routes behavior for text commands
    try:
        from deja.activity_log import append_log_entry

        append_log_entry(
            "command",
            f"{cmd_type} (spoken): {confirmation or transcript[:120]}",
        )
    except Exception:
        log.debug("activity_log append failed for voice command", exc_info=True)

    # Context-type commands fire an immediate integrate trigger so the
    # new signal gets processed against any other unprocessed signals.
    # (Non-context types already wrote their own state — tasks, actions
    # execute synchronously — and don't need an integrate cycle.)
    if cmd_type == "context":
        try:
            from deja.agent.analysis_cycle import trigger_integrate_now

            asyncio.create_task(
                trigger_integrate_now(reason="voice_transcript_context")
            )
        except Exception:
            log.debug(
                "immediate integrate trigger failed for voice context",
                exc_info=True,
            )

    response = CommandResponse(
        ok=True,
        type=cmd_type,
        confirmation=confirmation,
        details=details,
        cost_usd=round(cost, 6),
        latency_ms=latency_ms,
    )

    # Return the CommandResponse fields PLUS the transcript + recording
    # metadata the existing UI code expects.
    return {
        **response.model_dump(),
        "recording": False,
        "started_at": started_at,
        "transcript": transcript,
    }


@router.post("/api/mic/start")
async def mic_start() -> dict:
    if _mic_state.get("recording"):
        return {
            "recording": True,
            "reason": "already recording",
            "started_at": _mic_state["started_at"],
        }

    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    wav_path = AUDIO_DIR / f"session-{int(time.time())}.wav"

    # Spawn DejaRecorder fresh per recording. It runs until SIGTERM,
    # which mic_stop sends. No persistent daemon, no warm-mic trick —
    # the macOS mic indicator goes dark the instant this process exits.
    try:
        recorder_path = _find_recorder()
    except FileNotFoundError as e:
        log.error("mic_start: %s", e)
        return {"recording": False, "error": "DejaRecorder binary not found"}

    try:
        proc = subprocess.Popen(
            [recorder_path, "--mic", str(wav_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        log.exception("mic_start: failed to spawn recorder")
        return {"recording": False, "error": str(e)[:200]}

    log.info("mic recorder started (pid=%d) → %s", proc.pid, wav_path.name)

    started_at = datetime.now(timezone.utc).isoformat()
    _mic_state["recording"] = True
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
        "recording": _mic_state.get("recording", False),
        "started_at": _mic_state.get("started_at"),
    }
