"""Claude shadow integrator — parallel call to `claude -p` with the integrate prompt.

Non-production: when ``INTEGRATE_CLAUDE_SHADOW`` is on, every integrate
cycle fires a Claude subprocess with the exact prompt Gemini received
and captures Claude's JSON output into the existing shadow-eval file.
Lets us diff Claude-vs-Gemini decisions over real cycles without
touching the production wiki-write path.

Contract: ``invoke_claude_shadow(prompt) -> str`` returns the raw
stdout of ``claude -p`` (expected to be JSON, since the prompt tells
the model to return JSON). The caller parses as JSON, same as it
already does for Gemini output.

Runs synchronously on a thread pool; the caller awaits it the same
way as any other shadow task. A 60-second timeout caps blast — if
Claude hangs we log and continue.

Implementation mirrors chief_of_staff._run_claude: same binary
discovery fallback, same PATH augmentation, same dangerously-skip-
permissions flag (the shadow output never touches disk or the wiki;
it's pure observation). MCP config is NOT attached — the shadow is
a prompt-output comparison, not a full agent loop.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

# Bumped from 60s after real-cycle shadow calls routinely exceeded it.
# Claude reading a full integrate prompt (goals + wiki + 5K+ tokens of
# signals) with reasoning and JSON generation is a 60-120s operation.
# 240s leaves headroom while still catching runaway invocations.
_SUBPROCESS_TIMEOUT_SEC = 240

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


def _run_claude_sync(prompt: str) -> str:
    """Spawn ``claude -p`` with the full integrate prompt as user message.

    We pass the prompt as the positional arg rather than stdin so the
    CLI treats it as a user message. Nothing goes on stdin. Returns
    raw stdout; caller handles JSON parse.
    """
    claude_bin = _claude_binary()
    if not claude_bin:
        raise RuntimeError("claude CLI not found on PATH or fallback locations")

    cmd = [
        claude_bin,
        "-p", prompt,
        "--dangerously-skip-permissions",
        "--output-format", "text",
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT_SEC,
            env=_build_env(),
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"claude integrate shadow timed out after {_SUBPROCESS_TIMEOUT_SEC}s")

    if proc.returncode != 0:
        raise RuntimeError(
            f"claude integrate shadow rc={proc.returncode}: {proc.stderr[:300]}"
        )
    return proc.stdout


async def invoke_claude_shadow(prompt: str) -> str:
    """Async wrapper — run the subprocess on a thread so the cycle isn't blocked.

    Same signature as ``GeminiClient._generate``: takes the full prompt,
    returns raw text (expected JSON). Caller parses + handles errors.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _run_claude_sync, prompt)


__all__ = ["invoke_claude_shadow"]
