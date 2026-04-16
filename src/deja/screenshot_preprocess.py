"""Preprocess raw screen OCR into a compact signal for Graphiti.

Raw screenshots produce 3000-5000 chars of OCR including UI chrome,
sidebars, menu bars — most of it noise. This module runs a cheap
gpt-4.1-nano call to extract only the substantive content: what the
user is doing, who's involved, and the key visible text.

Output is either:
- A structured compact summary (~300-500 chars) ready for Graphiti
- The sentinel "SKIP" when there's nothing substantive to remember

Cost: ~$0.0005 per screenshot, drops Graphiti's per-episode cost ~10x.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

# System prompt — explicit about what to keep and what to drop.
_SYSTEM_PROMPT = """You preprocess screen OCR for a personal knowledge graph. Extract only
the substantive content the user is engaging with. Be concise.

Return a compact signal in this format:
CONTEXT: <what is the user doing? 1 sentence>
PEOPLE: <comma-separated names involved, or "none">
CONTENT:
<the substantive visible text — messages, email body, document paragraph,
form content. SKIP: app chrome, menu bars, sidebars, tab strips, button
labels, notification counts, timestamps, scroll positions, empty states>

If the screen has no substantive content (blank screen, lock screen, pure
UI chrome, Finder showing a folder, Terminal at a prompt, app switcher,
desktop, dock only), return the single word: SKIP"""

# Reuse the client across calls — building it is cheap but repeated
# module-level construction is pointless.
_client = None
_client_init_attempted = False


def _get_api_key() -> str | None:
    """Load OPENAI_API_KEY from env or ~/.deja/openai_key.

    Same fallback graphiti_ingest uses: macOS `open` doesn't inherit shell
    env vars, so the app process may not see OPENAI_API_KEY even when it
    is set in the user's shell.
    """
    api_key = os.environ.get("OPENAI_API_KEY")
    if api_key:
        return api_key
    key_file = Path.home() / ".deja" / "openai_key"
    if key_file.is_file():
        try:
            return key_file.read_text().strip() or None
        except OSError:
            return None
    return None


def _get_client():
    """Lazy-build an AsyncOpenAI client. Returns None when no key is set."""
    global _client, _client_init_attempted
    if _client is not None:
        return _client
    if _client_init_attempted:
        return None
    _client_init_attempted = True
    api_key = _get_api_key()
    if not api_key:
        log.warning(
            "screenshot_preprocess: OPENAI_API_KEY not set and "
            "~/.deja/openai_key not found — preprocessing disabled, raw OCR will flow through"
        )
        return None
    try:
        from openai import AsyncOpenAI
        _client = AsyncOpenAI(api_key=api_key)
        return _client
    except Exception:
        log.warning("screenshot_preprocess: failed to construct OpenAI client", exc_info=True)
        return None


async def preprocess_screenshot(
    ocr_text: str,
    app_name: str = "",
    window_title: str = "",
) -> str | None:
    """Preprocess OCR text into a compact structured signal.

    Returns:
        - None if the screen is pure chrome / empty (the "SKIP" sentinel)
        - A compact structured string otherwise (CONTEXT/PEOPLE/CONTENT)
        - The original OCR text on any error (fail-open — never block the pipeline)

    Uses gpt-4.1-nano (cheapest tier). ~$0.0005/call.
    """
    if not ocr_text or not ocr_text.strip():
        return None

    client = _get_client()
    if client is None:
        # No API key — degrade to raw OCR so the graphiti path still works.
        return ocr_text

    user_message = (
        f"App: {app_name}\n"
        f"Window: {window_title}\n\n"
        f"OCR text:\n{ocr_text}"
    )

    try:
        resp = await asyncio.wait_for(
            client.chat.completions.create(
                model="gpt-4.1-nano",
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": user_message},
                ],
                max_tokens=500,
                temperature=0.0,
            ),
            timeout=10.0,
        )
    except asyncio.TimeoutError:
        log.warning("screenshot_preprocess: timeout, falling back to raw OCR")
        return ocr_text
    except Exception:
        log.warning("screenshot_preprocess: API call failed, falling back to raw OCR", exc_info=True)
        return ocr_text

    try:
        content = (resp.choices[0].message.content or "").strip()
    except (AttributeError, IndexError):
        log.warning("screenshot_preprocess: malformed response, falling back to raw OCR")
        return ocr_text

    if not content:
        log.warning("screenshot_preprocess: empty content, falling back to raw OCR")
        return ocr_text

    # Treat any response that is exactly SKIP (or trivially so) as a skip
    # sentinel. Be tolerant: the model occasionally wraps it in quotes or
    # adds a trailing period.
    stripped = content.strip().strip('"').strip("'").rstrip(".").strip()
    if stripped.upper() == "SKIP":
        return None

    return content
