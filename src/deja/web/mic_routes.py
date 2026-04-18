"""Microphone recording endpoints.

POST /api/mic/start  — begin push-to-record session
POST /api/mic/stop   — end session, transcribe via Groq Whisper, dispatch
GET  /api/mic/status — {recording, started_at}

Recording runs inside the main Deja.app Swift process (see
`menubar/Sources/Services/VoiceCommandDispatcher.swift` +
`VoiceRecorder.swift`). We drive it via a file-marker protocol:

    Python → Swift  ~/.deja/voice_cmd.json
    Swift  → Python ~/.deja/voice_status.json

Why this exists: the old path spawned `DejaRecorder --mic` as a
subprocess, but DejaRecorder is a command-line tool with no bundle
identifier and no NSMicrophoneUsageDescription, so macOS TCC didn't
recognize it as holding mic permission — AVAudioEngine returned
zero-filled buffers and Whisper transcribed the silence as "you".
Running in-process means ONE mic TCC entry (com.deja.app).
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException

from deja.config import DEJA_HOME
from deja.observability import (
    DejaError,
    LLMError,
    ProxyUnavailable,
    ToolError,
    report_error,
    request_scope,
)
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
        try:
            resp = await client.post(
                f"{DEJA_API_URL}/v1/transcribe",
                headers=headers,
                files={"file": ("audio.wav", audio_bytes, "audio/wav")},
            )
        except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as e:
            raise ProxyUnavailable(
                f"transcribe request failed: {type(e).__name__}: {e}",
                details={"url": f"{DEJA_API_URL}/v1/transcribe"},
            ) from e
        if resp.status_code in (502, 503, 504):
            raise ProxyUnavailable(
                f"transcribe proxy returned {resp.status_code}",
                details={
                    "url": f"{DEJA_API_URL}/v1/transcribe",
                    "http_status": resp.status_code,
                },
            )
        if resp.status_code >= 400:
            raise LLMError(
                f"transcribe proxy returned {resp.status_code}",
                details={
                    "url": f"{DEJA_API_URL}/v1/transcribe",
                    "http_status": resp.status_code,
                    "body": resp.text[:500],
                },
            )
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

# File-marker IPC paths. Swift side: VoiceCommandDispatcher.swift.
VOICE_CMD_PATH = DEJA_HOME / "voice_cmd.json"
VOICE_STATUS_PATH = DEJA_HOME / "voice_status.json"


def _write_voice_cmd(action: str, **extra: str) -> str:
    """Write a command for the Swift VoiceCommandDispatcher to pick up.

    Captures the pre-existing voice_status.json ts (if any) so the
    caller can detect a *new* status write from Swift rather than
    matching a stale marker from a previous recording.
    """
    # Snapshot the status ts BEFORE writing the command — this is what
    # we compare against to detect Swift's response.
    prev_ts = ""
    prev = _read_voice_status()
    if prev:
        prev_ts = prev.get("ts", "")

    ts = datetime.now(timezone.utc).isoformat()
    payload = {"action": action, "ts": ts, **extra}
    DEJA_HOME.mkdir(parents=True, exist_ok=True)
    tmp = VOICE_CMD_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload))
    tmp.replace(VOICE_CMD_PATH)
    return prev_ts


def _read_voice_status() -> dict | None:
    if not VOICE_STATUS_PATH.exists():
        return None
    try:
        return json.loads(VOICE_STATUS_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return None


async def _await_voice_status(
    expected: str,
    *,
    after_ts: str,
    timeout: float,
    poll_interval: float = 0.1,
) -> dict:
    """Wait for voice_status.json to show ``expected`` with a ts different
    from ``after_ts`` (the snapshot taken just before we wrote the cmd).

    ts comparison is by inequality, not ordering — Swift's
    ISO8601DateFormatter and Python's datetime.isoformat() produce
    different tail suffixes (`Z` vs `+00:00`), so lexicographic
    ordering across the boundary is unreliable. An inequality check is
    enough because the dispatcher writes a fresh ts every time it
    handles a command.

    Raises HTTPException(500) on timeout or on an "error" status from
    Swift. No silent fallbacks — if the recorder didn't come up, we
    surface it loudly.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = _read_voice_status()
        if status:
            status_ts = status.get("ts", "")
            if status_ts and status_ts != after_ts:
                name = status.get("status", "")
                if name == expected:
                    return status
                if name == "error":
                    detail = status.get("detail", "unknown error")
                    raise HTTPException(
                        status_code=500,
                        detail=f"voice recorder error: {detail}",
                    )
        await asyncio.sleep(poll_interval)

    raise HTTPException(
        status_code=500,
        detail=(
            f"voice recorder did not reach status={expected!r} within "
            f"{timeout}s — is the Deja.app Swift process running?"
        ),
    )


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
    "recording": False,
    "wav_path": None,
    "started_at": None,
    "auto_stop_task": None,
}


async def _auto_stop_after(delay: float) -> None:
    """Safety net: stop the mic session after *delay* seconds if still running."""
    try:
        await asyncio.sleep(delay)
    except asyncio.CancelledError:
        return
    if _mic_state.get("recording"):
        await _mic_stop_inner(reason="auto-stop (safety timeout)")


async def _mic_stop_inner(reason: str = "manual") -> dict:
    """Stop recording via the Swift dispatcher, transcribe, dispatch. Idempotent."""
    wav_path: Path | None = _mic_state.get("wav_path")
    started_at: str | None = _mic_state.get("started_at")

    task = _mic_state.get("auto_stop_task")
    if task is not None and not task.done():
        task.cancel()
    _mic_state["auto_stop_task"] = None

    if not _mic_state.get("recording") or wav_path is None:
        _mic_state["recording"] = False
        _mic_state["wav_path"] = None
        _mic_state["started_at"] = None
        return {"recording": False, "reason": "no active session"}

    # Ask Swift to stop the in-process VoiceRecorder and flush the WAV.
    # Wait up to 5s for `done`; raise on timeout (no silent fallback).
    cmd_ts = _write_voice_cmd("stop")
    await _await_voice_status("done", after_ts=cmd_ts, timeout=5.0)

    _mic_state["recording"] = False
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

    # Probe the WAV for duration + RMS level so we can distinguish
    # "audio was captured but too quiet" from "audio was fine but
    # Whisper returned garbage" in the log when transcription flops.
    wav_duration = 0.0
    wav_rms_db = None
    wav_sample_rate = 0
    try:
        import wave
        with wave.open(str(wav_path), "rb") as wf:
            wav_sample_rate = wf.getframerate()
            wav_nframes = wf.getnframes()
            if wav_sample_rate > 0:
                wav_duration = wav_nframes / wav_sample_rate
            # Read up to 10s of samples to compute RMS
            sample_width = wf.getsampwidth()
            n_read = min(wav_nframes, wav_sample_rate * 10)
            frames = wf.readframes(n_read)
        if sample_width == 2 and frames:
            import array, math
            samples = array.array("h")
            samples.frombytes(frames)
            if samples:
                sq = sum(s * s for s in samples) / len(samples)
                if sq > 0:
                    rms = math.sqrt(sq)
                    # dBFS: 0 = full scale (32768), -inf = silence
                    wav_rms_db = 20 * math.log10(rms / 32768.0)
    except Exception:
        log.debug("mic_stop: wav probe failed", exc_info=True)

    log.info(
        "mic_stop: wav=%s size=%d bytes duration=%.1fs sr=%d rms=%.1fdBFS",
        wav_path, wav_size, wav_duration, wav_sample_rate,
        wav_rms_db if wav_rms_db is not None else float("nan"),
    )
    if wav_rms_db is not None and wav_rms_db < -50:
        log.warning(
            "mic_stop: audio is very quiet (%.1f dBFS) — mic level may be too low",
            wav_rms_db,
        )

    # Transcribe via Groq Whisper (fast, dedicated speech-to-text).
    transcript = ""
    transcribe_error = None
    try:
        transcript = await _transcribe_groq(wav_path)
    except DejaError as err:
        # Typed proxy/upstream failure — report visibly so the pill
        # shows a real error toast instead of silently dropping the
        # user's dictation.
        transcribe_error = err.user_message
        err.details.setdefault("phase", "transcribe")
        report_error(err, visible_to_user=True)
    except Exception as e:
        transcribe_error = str(e)
        log.exception("mic_stop: transcription failed")
        report_error(
            ToolError(
                f"transcription failed: {type(e).__name__}: {e}",
                details={"phase": "transcribe", "exception_type": type(e).__name__},
            ),
            visible_to_user=True,
        )

    # Keep the WAV for debug whenever transcription is empty OR
    # flagged as a Whisper hallucination (filter below). On clean
    # transcripts we unlink at the very end of the function.

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
        # Keep the WAV on disk for diagnostic inspection — DO NOT unlink.
        # The file path is included in the response so a developer can
        # check the audio directly.
        log.info(
            "mic_stop: filtered Whisper hallucination: %r (wav kept at %s)",
            raw, wav_path,
        )
        # Audit entry so the dropped transcription is traceable.
        try:
            from deja import audit

            kb = wav_size // 1024
            rms_str = (
                f" ({wav_rms_db:.1f}dBFS)"
                if wav_rms_db is not None else ""
            )
            audit.record(
                "voice_transcript",
                target="mic/dropped",
                reason=(
                    f"Whisper hallucination: {raw!r} "
                    f"({kb}KB audio{rms_str})"
                ),
                trigger={"kind": "user_cmd", "detail": "voice"},
            )
        except Exception:
            log.debug("audit.record failed for dropped transcription", exc_info=True)
        return {
            "ok": False,
            "recording": False,
            "reason": "transcription_dropped",
            "detail": "Couldn't understand audio — try speaking louder or closer to the mic.",
            "raw_transcript": raw,
            "wav_size_bytes": wav_size,
            "wav_path": str(wav_path),
            "wav_duration_sec": round(wav_duration, 1),
            "wav_rms_dbfs": round(wav_rms_db, 1) if wav_rms_db is not None else None,
        }

    if not transcript:
        # Empty transcript — keep the WAV for debug too.
        log.warning("mic_stop: empty transcript, wav kept at %s", wav_path)
        return {
            "ok": False,
            "recording": False,
            "reason": reason,
            "transcript": "",
            "error": transcribe_error or "no speech detected",
            "wav_path": str(wav_path),
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

    # 2. Route directly to cos (Claude Opus) — the single most powerful
    #    routing step, no separate classifier. Cos reads the transcript,
    #    decides what to do (action, goal, wiki update, context, query),
    #    executes via MCP, and returns a short reply that we surface in
    #    the pill. The user's utterance + cos's reply land in the
    #    conversations/ store so future cos cycles have continuity.
    try:
        from deja import chief_of_staff
    except Exception:
        log.exception("mic_stop: failed to import chief_of_staff")
        raise

    if not chief_of_staff.is_enabled():
        return {
            "ok": False,
            "recording": False,
            "reason": "cos_disabled",
            "detail": "Chief of staff is disabled. Run `deja cos enable`.",
            "transcript": transcript,
        }

    loop = asyncio.get_running_loop()
    try:
        rc, cos_reply, stderr = await loop.run_in_executor(
            None,
            lambda: chief_of_staff.invoke_command_sync(
                user_message=transcript,
                source="voice",
            ),
        )
    except Exception as e:
        log.exception("mic_stop: cos command invocation failed")
        wrapped = LLMError(
            f"cos command failed: {type(e).__name__}: {e}",
            details={"phase": "cos_command", "exception_type": type(e).__name__},
        )
        report_error(wrapped, visible_to_user=True)
        return {
            "ok": False,
            "recording": False,
            "reason": "cos_command_failed",
            "detail": f"Cos invocation failed: {e}",
            "transcript": transcript,
        }

    try:
        from deja import audit
        audit.record(
            "voice_transcript",
            target="command/cos",
            reason=(cos_reply or transcript)[:200],
            trigger={"kind": "user_cmd", "detail": "voice"},
        )
    except Exception:
        log.debug("audit.record failed for voice command", exc_info=True)

    try:
        wav_path.unlink(missing_ok=True)
    except Exception:
        log.debug("mic_stop: wav cleanup failed", exc_info=True)

    return {
        "ok": rc == 0,
        "recording": False,
        "started_at": started_at,
        "transcript": transcript,
        "cos_response": cos_reply,
        "stderr": stderr[-400:] if stderr else "",
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

    # Drive the in-process Swift VoiceRecorder via the voice_cmd.json
    # marker. Wait up to 2s for Swift to echo back `recording` — if it
    # doesn't, raise loudly (no silent fallback).
    cmd_ts = _write_voice_cmd("start", wav_path=str(wav_path))
    await _await_voice_status("recording", after_ts=cmd_ts, timeout=2.0)

    log.info("voice recording started → %s", wav_path.name)

    started_at = datetime.now(timezone.utc).isoformat()
    _mic_state["recording"] = True
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
    # Wrap in a request_scope so voice transcription + classification +
    # audit writes all share one correlation id that any error report
    # picks up automatically.
    with request_scope():
        return await _mic_stop_inner(reason="manual")


@router.get("/api/mic/status")
def mic_status() -> dict:
    return {
        "recording": _mic_state.get("recording", False),
        "started_at": _mic_state.get("started_at"),
    }
