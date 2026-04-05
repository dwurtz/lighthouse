"""FastAPI backend for the Lighthouse notch app.

Endpoints:
  - GET  /api/status          — liveness + last signal/analysis timestamps
  - GET  /api/chat            — load chat history
  - POST /api/chat            — send a message (streams, writes exchange to
                                signal_log, lets the next cycle decide what
                                to do with it)
  - GET  /api/contacts/search — @mention autocomplete for the chat input
  - POST /api/mic/start       — begin push-to-record session (ffmpeg + TCC)
  - POST /api/mic/stop        — end session, transcribe via Gemini native
                                audio, emit as a source="microphone" signal
  - GET  /api/mic/status      — {recording, started_at, auto_stop_at}

Everything else the Swift notch reads comes straight from disk
(observations.jsonl, integrations.jsonl, wiki pages) — no HTTP involved.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal as _signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

from lighthouse.config import LIGHTHOUSE_HOME
from lighthouse.prompts import load as load_prompt

app = FastAPI(title="lighthouse", version="0.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

OBSERVATIONS_LOG = LIGHTHOUSE_HOME / "observations.jsonl"
INTEGRATIONS_LOG = LIGHTHOUSE_HOME / "integrations.jsonl"
CONVERSATION_PATH = LIGHTHOUSE_HOME / "conversation.json"


# ---------------------------------------------------------------------------
# Tiny helpers
# ---------------------------------------------------------------------------


def _read_jsonl(path: Path, limit: int | None = None) -> list[dict]:
    if not path.exists():
        return []
    entries: list[dict] = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    if limit is not None:
        entries = entries[-limit:]
    return entries


def _load_conversation() -> list[dict]:
    if not CONVERSATION_PATH.exists():
        return []
    try:
        return json.loads(CONVERSATION_PATH.read_text())
    except (json.JSONDecodeError, ValueError):
        return []


def _save_conversation(messages: list[dict]) -> None:
    CONVERSATION_PATH.write_text(json.dumps(messages, indent=2))


# ---------------------------------------------------------------------------
# GET /api/status — liveness probe
# ---------------------------------------------------------------------------


@app.get("/api/status")
def get_status() -> dict:
    """Return liveness info. `monitor_running` is true if a signal landed in
    the last 120s. Compares UTC-aware to UTC-aware (handling naive local
    timestamps in the signal log by attaching the local tz)."""
    last_signal_time = None
    if OBSERVATIONS_LOG.exists():
        with open(OBSERVATIONS_LOG, "rb") as f:
            try:
                f.seek(-4096, 2)
            except OSError:
                f.seek(0)
            tail = f.read().decode("utf-8", errors="replace").splitlines()
        for line in reversed(tail):
            line = line.strip()
            if not line:
                continue
            try:
                last_signal_time = json.loads(line).get("timestamp")
                break
            except json.JSONDecodeError:
                continue

    last_analysis_time = None
    if INTEGRATIONS_LOG.exists():
        with open(INTEGRATIONS_LOG, "rb") as f:
            try:
                f.seek(-4096, 2)
            except OSError:
                f.seek(0)
            tail = f.read().decode("utf-8", errors="replace").splitlines()
        for line in reversed(tail):
            line = line.strip()
            if not line:
                continue
            try:
                last_analysis_time = json.loads(line).get("timestamp")
                break
            except json.JSONDecodeError:
                continue

    monitor_running = False
    if last_signal_time:
        last_dt = datetime.fromisoformat(last_signal_time.replace("Z", "+00:00"))
        if last_dt.tzinfo is None:
            last_dt = last_dt.astimezone()
        age = (datetime.now(timezone.utc) - last_dt).total_seconds()
        monitor_running = age < 120

    return {
        "monitor_running": monitor_running,
        "last_signal_time": last_signal_time,
        "last_analysis_time": last_analysis_time,
    }


# ---------------------------------------------------------------------------
# GET /api/contacts/search — @mention autocomplete
# ---------------------------------------------------------------------------


@app.get("/api/contacts/search")
def search_contacts(q: str = Query(""), limit: int = Query(10)) -> list[dict]:
    if not q:
        return []
    from lighthouse.observations import contacts as contacts_mod

    if contacts_mod._name_set is None:
        contacts_mod._build_index()
    names = contacts_mod._name_set or set()
    phones = contacts_mod._phone_index or {}

    query = q.lower().strip()
    matches: list[dict] = []
    seen: set[str] = set()
    for name in sorted(names):
        if query in name and name not in seen:
            # Find phones associated with this name
            matching_phones = [p for p, n in phones.items() if n.lower() == name]
            matches.append({
                "name": name.title(),
                "phones": matching_phones[:2],
                "emails": [],
                "goals": [],
            })
            seen.add(name)
            if len(matches) >= limit:
                break
    return matches


# ---------------------------------------------------------------------------
# GET /api/chat — load history
# POST /api/chat — send message, stream reply, write exchange to signal_log
# ---------------------------------------------------------------------------


@app.get("/api/chat")
def get_chat(limit: int = 50) -> list[dict]:
    return _load_conversation()[-limit:]


@app.post("/api/chat")
async def post_chat(body: dict):
    """Chat with Pro, with free-reign wiki tool access.

    The model can call ``list_pages`` / ``read_page`` / ``write_page`` /
    ``delete_page`` / ``rename_page`` directly — the user saying "rename
    coach-rob to robert-toy and delete terafab" translates to a planned
    sequence of tool calls executed synchronously in this request. The
    agentic loop runs until Pro stops emitting function calls, then its
    final text reply is streamed to the client.

    Safety posture: every tool validates category/slug, stays inside
    WIKI_DIR, and logs each mutation to log.md and lighthouse.log. The
    wiki is a git repo with per-change auto-commits (via the reflect
    cycle and manual operations), so any bad call is reversible via git
    revert.
    """
    from starlette.responses import StreamingResponse

    user_message = body.get("message", "")
    exchange_start_ts = datetime.now(timezone.utc).isoformat()

    messages = _load_conversation()
    messages.append({
        "role": "user",
        "content": user_message,
        "timestamp": exchange_start_ts,
    })
    _save_conversation(messages)

    from lighthouse.llm_client import GeminiClient
    gemini = GeminiClient()

    # Relevant wiki pages via QMD — keeps chat context focused, no whole-wiki dump.
    from lighthouse.llm.search import search as qmd_search
    wiki_text = qmd_search(user_message, limit=5, collection="wiki")

    # Recent observations as peripheral context.
    observations = _read_jsonl(OBSERVATIONS_LOG)
    recent_observations = ""
    for s in observations[-30:]:
        recent_observations += (
            f"[{s.get('timestamp', '')[:19]}] [{s.get('source', '')}] "
            f"{s.get('sender', '')}: {s.get('text', '')[:150]}\n"
        )

    from lighthouse.identity import load_user
    user = load_user()
    user_fields = user.as_prompt_fields()

    history_lines = []
    for msg in messages[-20:]:
        role = user.first_name if msg["role"] == "user" else "Assistant"
        history_lines.append(f"{role}: {msg['content']}")

    from lighthouse.wiki_schema import load_schema
    schema = load_schema()

    system_instruction = load_prompt("chat").format(
        schema=schema,
        wiki=wiki_text or "(no relevant pages)",
        recent_observations=recent_observations[-3000:] or "(none)",
        history=chr(10).join(history_lines[-10:]),
        **user_fields,
    )

    # Tool-enabled chat loop. Each iteration: Pro sees the running
    # conversation + any tool results so far, decides whether to emit
    # more function calls or a final text reply. We stream events to
    # the client for each tool call so the UI can show progress.
    from lighthouse.chat_tools import build_tool_declarations, execute_tool_call
    from google.genai import types as genai_types

    tools = build_tool_declarations()

    # Seed the contents with the current user turn — the system prompt
    # already encodes history and wiki context.
    contents: list = [
        genai_types.Content(
            role="user",
            parts=[genai_types.Part.from_text(text=user_message)],
        ),
    ]

    MAX_TOOL_ROUNDS = 8  # hard ceiling to prevent infinite loops

    async def stream_response():
        full_text_response = ""
        tool_events: list[dict] = []

        for round_idx in range(MAX_TOOL_ROUNDS):
            try:
                resp = await gemini.client.aio.models.generate_content(
                    model="gemini-2.5-pro",
                    contents=contents,
                    config=genai_types.GenerateContentConfig(
                        system_instruction=system_instruction,
                        tools=tools,
                        max_output_tokens=4096,
                        temperature=0.4,
                    ),
                )
            except Exception as e:
                err = f"chat LLM call failed: {e}"
                log.exception(err)
                yield f"data: {json.dumps({'chunk': f'[error] {err}'})}\n\n"
                break

            # Collect function calls + text from this round.
            function_calls = []
            round_text = ""
            candidate = resp.candidates[0] if resp.candidates else None
            parts = (candidate.content.parts if candidate and candidate.content else []) or []
            for part in parts:
                if getattr(part, "function_call", None):
                    fc = part.function_call
                    function_calls.append(fc)
                elif getattr(part, "text", None):
                    round_text += part.text

            # Append the model's turn to contents so the next round sees it.
            if parts:
                contents.append(
                    genai_types.Content(role="model", parts=parts)
                )

            # If the model produced narration text, stream it to the client
            # before executing tool calls so the user sees Pro's intent.
            if round_text:
                full_text_response += round_text
                yield f"data: {json.dumps({'chunk': round_text})}\n\n"

            # No tool calls → Pro is done, end the loop.
            if not function_calls:
                break

            # Execute each tool call and append the response parts.
            response_parts = []
            for fc in function_calls:
                name = fc.name
                args = dict(fc.args) if fc.args else {}
                result = execute_tool_call(name, args)
                tool_events.append({
                    "tool": name,
                    "args": {k: (v if not isinstance(v, str) or len(v) < 200 else v[:200] + "…") for k, v in args.items()},
                    "ok": result.ok,
                    "message": result.message,
                })
                # Stream a tool-call event so the UI can render it inline.
                yield f"data: {json.dumps({'tool_call': tool_events[-1]})}\n\n"

                response_parts.append(
                    genai_types.Part.from_function_response(
                        name=name,
                        response=result.as_response_dict(),
                    )
                )

            contents.append(genai_types.Content(role="user", parts=response_parts))
        else:
            # Loop hit the round ceiling without Pro finishing.
            warning = f"[chat tool loop hit {MAX_TOOL_ROUNDS}-round ceiling]"
            log.warning(warning)
            yield f"data: {json.dumps({'chunk': warning})}\n\n"

        # If any mutations happened, commit them as one chat turn.
        mutating = [e for e in tool_events if e["ok"] and e["tool"] in ("write_page", "delete_page", "rename_page")]
        if mutating:
            try:
                from lighthouse.wiki_git import commit_changes
                from lighthouse.wiki_catalog import rebuild_index
                rebuild_index()
                summary = "; ".join(f"{e['tool']} {e['args'].get('slug') or e['args'].get('old_slug','?')}" for e in mutating[:5])
                if len(mutating) > 5:
                    summary += f" (+{len(mutating) - 5} more)"
                commit_changes(f"chat: {summary}")
            except Exception:
                log.exception("chat post-commit failed")

        # Save the full assistant turn to conversation.json.
        messages.append({
            "role": "agent",
            "content": full_text_response or "(no narration)",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "tool_calls": tool_events or None,
        })
        _save_conversation(messages)

        # Write a single chat observation so reflect can see the exchange.
        now_ts = datetime.now(timezone.utc).isoformat()
        tool_summary = ""
        if tool_events:
            tool_summary = "\n  Tools: " + "; ".join(
                f"{e['tool']}({'ok' if e['ok'] else 'fail'})" for e in tool_events
            )
        exchange_text = (
            f"CONVERSATION with Agent (1 exchange, {exchange_start_ts[11:16]}-{now_ts[11:16]}):\n"
            f"  {user.first_name}: {user_message}\n"
            f"  Agent: {full_text_response}{tool_summary}"
        )
        id_key = "chat-" + hashlib.md5(
            f"{exchange_start_ts}-{user_message[:100]}-{full_text_response[:100]}".encode()
        ).hexdigest()[:16]
        try:
            with open(OBSERVATIONS_LOG, "a") as f:
                f.write(json.dumps({
                    "source": "chat",
                    "sender": f"Agent ↔ {user.first_name}",
                    "text": exchange_text[:2000],
                    "timestamp": exchange_start_ts,
                    "id_key": id_key,
                }) + "\n")
        except Exception:
            log.exception("failed to persist chat observation")

        # Human-readable entry in log.md.
        from lighthouse.activity_log import append_log_entry
        one_line_user = " ".join((user_message or "").split())[:120]
        suffix = f" [{len(mutating)} wiki edit(s)]" if mutating else ""
        append_log_entry("chat", f"{user.first_name}: {one_line_user}{suffix}")

        yield f"data: {json.dumps({'done': True})}\n\n"

    return StreamingResponse(stream_response(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# POST /api/mic/start         — begin push-to-record session
# POST /api/mic/stop          — end session, transcribe, emit signal
# GET  /api/mic/status        — current recording state
#
# Design: the Swift menu bar item toggles between "Start Listening" and
# "Stop Listening". Click start → POST /api/mic/start → web server spawns
# ffmpeg via avfoundation to capture the default microphone into a WAV in
# ~/.lighthouse/audio/. Click stop → POST /api/mic/stop → web server sends
# SIGINT to ffmpeg (which writes the WAV trailer cleanly), reads the file,
# uploads to gemini-2.5-flash-native-audio-latest for transcription, and
# appends the transcript as a source="microphone" signal to observations.jsonl.
# The next 5-min analysis cycle picks it up like any other source.
#
# Safety: if the user forgets to stop, we auto-stop after MIC_AUTO_STOP_SEC.
# The ffmpeg binary path is stable so macOS TCC remembers the grant across
# restarts after the first permission prompt.
# ---------------------------------------------------------------------------

AUDIO_DIR = LIGHTHOUSE_HOME / "audio"
MIC_AUTO_STOP_SEC = 300  # 5 minutes — user probably forgot

_mic_state: dict = {
    "process": None,      # subprocess.Popen | None
    "wav_path": None,     # Path | None
    "started_at": None,   # ISO timestamp | None
    "auto_stop_task": None,  # asyncio.Task | None
}


async def _auto_stop_after(delay: float) -> None:
    """Safety net: kill the mic session after `delay` seconds if still running."""
    try:
        await asyncio.sleep(delay)
    except asyncio.CancelledError:
        return
    if _mic_state["process"] is not None:
        await _mic_stop_inner(reason="auto-stop (safety timeout)")


async def _mic_stop_inner(reason: str = "manual") -> dict:
    """Stop the current ffmpeg process and transcribe the resulting WAV.

    Returns a summary dict. Clears mic state on exit. Idempotent: if no
    recording is active, returns {"recording": False} without error.
    """
    proc: subprocess.Popen | None = _mic_state.get("process")
    wav_path: Path | None = _mic_state.get("wav_path")
    started_at: str | None = _mic_state.get("started_at")

    # Cancel the auto-stop watchdog first so it doesn't re-enter.
    task = _mic_state.get("auto_stop_task")
    if task is not None and not task.done():
        task.cancel()
    _mic_state["auto_stop_task"] = None

    if proc is None or wav_path is None:
        _mic_state["process"] = None
        _mic_state["wav_path"] = None
        _mic_state["started_at"] = None
        return {"recording": False, "reason": "no active session"}

    # Tell ffmpeg to flush and exit cleanly. SIGINT is gentler than TERM
    # for ffmpeg — it writes the WAV trailer and exits with code 255.
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

    import logging
    mic_log = logging.getLogger("lighthouse.mic")

    if not wav_path.exists() or wav_path.stat().st_size < 4096:
        size = wav_path.stat().st_size if wav_path.exists() else 0
        mic_log.warning("mic_stop: wav too small (%d bytes) at %s", size, wav_path)
        try:
            wav_path.unlink(missing_ok=True)
        except Exception:
            pass
        return {"recording": False, "reason": f"no audio captured ({reason})"}

    mic_log.info("mic_stop: wav=%s size=%d bytes", wav_path, wav_path.stat().st_size)

    # Transcribe via Gemini.
    from lighthouse.llm_client import GeminiClient
    gemini = GeminiClient()
    try:
        transcript = await gemini.transcribe_audio(str(wav_path))
    except Exception as e:
        transcript = ""
        transcribe_error = str(e)
        mic_log.exception("mic_stop: transcription failed")
    else:
        transcribe_error = None

    # Keep the WAV around for debugging if the transcript is empty. On a
    # successful transcription we delete it.
    if transcript:
        try:
            wav_path.unlink(missing_ok=True)
        except Exception:
            pass
    else:
        mic_log.warning(
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

    # Emit as a signal. Same format as chat signals — written directly to
    # observations.jsonl so the next analysis cycle processes it.
    ts = datetime.now(timezone.utc).isoformat()
    id_key = "mic-" + hashlib.md5(f"{ts}-{transcript[:200]}".encode()).hexdigest()[:16]
    try:
        OBSERVATIONS_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(OBSERVATIONS_LOG, "a") as f:
            f.write(json.dumps({
                "source": "microphone",
                "sender": "David (spoken)",
                "text": transcript[:2000],
                "timestamp": ts,
                "id_key": id_key,
            }) + "\n")
    except Exception:
        pass

    # Human-readable log in the wiki
    try:
        from lighthouse.activity_log import append_log_entry
        preview = " ".join(transcript.split())[:120]
        append_log_entry("mic", f"David (spoken): {preview}")
    except Exception:
        pass

    return {
        "recording": False,
        "reason": reason,
        "started_at": started_at,
        "transcript": transcript,
    }


@app.post("/api/mic/start")
async def mic_start() -> dict:
    if _mic_state["process"] is not None:
        return {"recording": True, "reason": "already recording", "started_at": _mic_state["started_at"]}

    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    wav_path = AUDIO_DIR / f"session-{int(time.time())}.wav"

    # ffmpeg avfoundation input: ":0" means "no video, default audio device".
    # -ar 16000 -ac 1: 16kHz mono, standard for speech recognition.
    # -y: overwrite if path exists.
    # -loglevel error: suppress the chatty banner.
    cmd = [
        "ffmpeg",
        "-f", "avfoundation",
        "-i", ":0",
        "-ar", "16000",
        "-ac", "1",
        "-y",
        "-loglevel", "error",
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

    # Safety watchdog — auto-stop after MIC_AUTO_STOP_SEC if still recording.
    _mic_state["auto_stop_task"] = asyncio.create_task(_auto_stop_after(MIC_AUTO_STOP_SEC))

    return {
        "recording": True,
        "started_at": started_at,
        "auto_stop_sec": MIC_AUTO_STOP_SEC,
    }


@app.post("/api/mic/stop")
async def mic_stop() -> dict:
    return await _mic_stop_inner(reason="manual")


@app.get("/api/mic/status")
def mic_status() -> dict:
    return {
        "recording": _mic_state["process"] is not None,
        "started_at": _mic_state.get("started_at"),
    }


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------


def run_web(port: int = 5055) -> None:
    """Start the web server. Called by `python -m lighthouse web`."""
    import uvicorn
    print(f"Lighthouse: http://localhost:{port}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    run_web()
