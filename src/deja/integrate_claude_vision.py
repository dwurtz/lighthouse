"""Claude vision shadow — send native screenshot PNGs instead of OCR text.

The third shadow variant in the integrate eval. Instead of passing
Claude the OCR-then-preprocess intermediate representation of what's
on screen, pass Claude the actual PNG via Anthropic's multimodal
content-block input. Claude reads pixels directly.

Why it matters
--------------

OCR + the preprocess VLM lose visual context that Claude can use:
distinguishing a focused/active window from an inbox list preview,
reading a calendar cell by its grid position rather than by
misinterpreted text, noticing visual emphasis (bold, gray, underline)
that indicates state. We saw concrete quality losses from the
lossy pipeline — the Sorvillo 4:30pm / noon confusion, the Mike Wur2
ghost page, the inbox-preview re-events.

Vision is more expensive (~1000 image tokens per screenshot,
Opus 4.7 reasoning), but the hypothesis is that it closes a quality
gap we can't close any other way.

Pipeline
--------

The observation log has one row per screenshot with an ``id_key``.
The raw image sidecar (``deja.raw_image_sidecar``) preserves the
corresponding PNG by id_key. For each screenshot in the cycle's
signals, we look up its image bytes, base64-encode, and embed as an
image content block in a stream-json user message. Non-screenshot
signals stay as text in the same message.

Claude CLI integration uses:

    claude -p \\
      --input-format stream-json \\
      --output-format stream-json --verbose \\
      --model claude-opus-4-7 \\
      --system-prompt <neutral> \\
      --dangerously-skip-permissions

The stream-json input is one JSON object per line, each
``{"type": "user", "message": {"role": "user", "content": [...]}}``.
We send a single user message with mixed text + image blocks.

The output is stream-json too (verbose required). We collect the
assistant text from ``assistant`` events and the final ``result``
event, returning the last ``result.result`` string (JSON text to
parse upstream).
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Iterable

log = logging.getLogger(__name__)

_SUBPROCESS_TIMEOUT_SEC = 360  # longer than text — images are 10× data
# Hard cap on how many images we'll attach per cycle. Even with Max,
# 30-image cycles would send ~30K image tokens per call; keep the
# attention budget reasonable.
_MAX_IMAGES_PER_CYCLE = 12
_SYSTEM_PROMPT = (
    "You are an observation integrator for a personal AI memory system. "
    "Follow the instructions in the user message exactly. Return ONLY the "
    "JSON object specified — no preamble, no postscript, no code fences. "
    "Do not use any tools; answer from the prompt text and attached images "
    "alone. Screenshots are authoritative visual context — use them to "
    "distinguish active-reading from inbox-list-preview, to resolve "
    "ambiguous pronouns to what's currently on screen, and to reject text "
    "that appears only as a background preview."
)

_CLAUDE_FALLBACK_PATHS = (
    "/Applications/cmux.app/Contents/Resources/bin/claude",
    str(Path.home() / ".local/bin/claude"),
    "/opt/homebrew/bin/claude",
    "/usr/local/bin/claude",
)

_PATH_EXTRAS = (
    "/usr/local/bin",
    "/usr/bin",
    "/bin",
    "/usr/sbin",
    "/sbin",
    "/opt/homebrew/bin",
    str(Path.home() / ".local/bin"),
    "/Applications/cmux.app/Contents/Resources/bin",
)


def _claude_binary() -> str | None:
    found = shutil.which("claude")
    if found:
        return found
    for candidate in _CLAUDE_FALLBACK_PATHS:
        if Path(candidate).exists() and os.access(candidate, os.X_OK):
            return candidate
    return None


def _build_env() -> dict:
    env = {**os.environ}
    existing = env.get("PATH", "")
    env["PATH"] = ":".join([*_PATH_EXTRAS, existing]) if existing else ":".join(_PATH_EXTRAS)
    env.setdefault("HOME", str(Path.home()))
    return env


def _collect_screenshot_images(signal_items: Iterable[dict]) -> list[tuple[str, bytes]]:
    """Return (id_key, png_bytes) for up to ``_MAX_IMAGES_PER_CYCLE``
    screenshot signals, favoring the MOST RECENT.

    A rapid-switching user can produce 20+ screenshots per cycle; the
    image token budget + Claude's attention are both bounded. We
    prioritize the most recent frames (closer to "what the user is
    doing right now") over the oldest. Observation signals carry an
    ISO timestamp; sort descending and take the top N.

    Graceful skips: missing sidecar, missing timestamp, missing id_key
    — we just drop that entry.
    """
    from deja.raw_image_sidecar import read_bytes as _read_img

    screenshots = [
        o for o in (signal_items or [])
        if o.get("source") == "screenshot" and o.get("id_key")
    ]
    # Sort newest-first by timestamp string (ISO lex-order is chrono).
    screenshots.sort(key=lambda o: o.get("timestamp") or "", reverse=True)

    out: list[tuple[str, bytes]] = []
    for obs in screenshots:
        if len(out) >= _MAX_IMAGES_PER_CYCLE:
            break
        data = _read_img(obs["id_key"])
        if not data:
            continue
        out.append((obs["id_key"], data))
    # Reverse to chronological so Claude sees them oldest→newest (the
    # natural narrative order). The "newest-first" sort was just for
    # the cap-respecting selection step.
    out.reverse()
    return out


def _build_stream_json_input(prompt_text: str, images: list[tuple[str, bytes]]) -> str:
    """Assemble the single-line stream-json user message with text + images."""
    content = [{"type": "text", "text": prompt_text}]
    for (id_key, data) in images:
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": base64.b64encode(data).decode(),
            },
        })
    return json.dumps({
        "type": "user",
        "message": {"role": "user", "content": content},
    }) + "\n"


def _extract_result_text(stdout: str) -> str:
    """Walk the stream-json output and return the final result text.

    Stream-json output is one JSON object per line. The ``result``
    event at the end carries the final text. If it's missing, fall
    back to concatenating assistant text blocks.
    """
    result_text = ""
    assistant_chunks: list[str] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
        except Exception:
            continue
        t = evt.get("type")
        if t == "result" and isinstance(evt.get("result"), str):
            result_text = evt["result"]
        elif t == "assistant":
            for c in evt.get("message", {}).get("content", []):
                if c.get("type") == "text" and c.get("text"):
                    assistant_chunks.append(c["text"])
    return result_text or "".join(assistant_chunks)


def _run_claude_vision_sync(
    prompt_text: str,
    signal_items: list[dict],
) -> str:
    """Spawn claude with stream-json input, return the final text (expected JSON)."""
    claude_bin = _claude_binary()
    if not claude_bin:
        raise RuntimeError("claude CLI not found")

    images = _collect_screenshot_images(signal_items)
    stdin = _build_stream_json_input(prompt_text, images)

    cmd = [
        claude_bin,
        "-p",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--verbose",
        "--model", "claude-opus-4-7",
        "--system-prompt", _SYSTEM_PROMPT,
        "--dangerously-skip-permissions",
    ]
    try:
        proc = subprocess.run(
            cmd,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT_SEC,
            env=_build_env(),
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"claude vision shadow timed out after {_SUBPROCESS_TIMEOUT_SEC}s "
            f"({len(images)} images)"
        )

    if proc.returncode != 0:
        raise RuntimeError(
            f"claude vision shadow rc={proc.returncode}: {proc.stderr[:400]}"
        )
    text = _extract_result_text(proc.stdout)
    if not text:
        raise RuntimeError(
            f"claude vision shadow returned no result text ({len(proc.stdout)} "
            f"bytes stdout, stderr={proc.stderr[:200]})"
        )
    log.info(
        "claude vision shadow: %d image(s), %d result chars",
        len(images), len(text),
    )
    return text


async def invoke_claude_vision_shadow(
    prompt_text: str,
    signal_items: list[dict],
) -> str:
    """Async wrapper — runs the subprocess on a thread so the cycle isn't blocked."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, _run_claude_vision_sync, prompt_text, signal_items
    )


__all__ = ["invoke_claude_vision_shadow"]
