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

# System prompt — classify the app/window type first, then decide if the
# content matters to David as a HUMAN (not as an engineer debugging his
# own tools). Reject meta-content aggressively: Terminals, Claude Code,
# log viewers, admin dashboards — all contaminate the personal graph.
_SYSTEM_PROMPT = """You preprocess screen OCR for David Wurtz's personal knowledge graph.
David is a builder/entrepreneur in Phoenix. The graph remembers things
that matter to his LIFE, RELATIONSHIPS, and WORK — not the plumbing of
his tools or his own debugging sessions.

You are given the app name, window title, and OCR text. Reason carefully.

STEP 1 — classify what's on screen (one of):
  • PERSONAL_COMM: Messages, WhatsApp, Signal, iMessage, FaceTime
  • EMAIL: Superhuman, Gmail, Mail.app, Outlook
  • DOCUMENT: Google Docs, Notion, Notes, Obsidian, Word, Pages
  • CALENDAR_PLANNING: Calendar, Linear, Things, Todoist, Asana
  • WEB_CONTENT: Safari, Chrome, Arc showing an actual article, tweet,
    video, or product page David is reading (NOT his own app's admin
    panel or dev tools)
  • WORK_CHAT: Slack, Discord, Teams — substantive work conversation
  • MEETING: Zoom, Meet, FaceTime active meeting
  • DEV_TOOL: Terminal, iTerm, VS Code, Xcode, Claude Code, Console,
    Activity Monitor, Docker Desktop, Redis CLI, logs, debug output,
    Deja's own UI, or screenshots of any AI assistant interface
  • ADMIN: System Settings, Finder browsing files, app switcher,
    Spotlight, desktop, dock, lock screen, empty windows
  • MEDIA: YouTube, Netflix, Spotify, Music, video players
  • OTHER: something that doesn't fit above

STEP 2 — decide:
  • DEV_TOOL, ADMIN → output exactly: SKIP
  • MEDIA → SKIP unless it's something David would remember
    (e.g., a specific YouTube video whose title clearly matters)
  • OTHER → SKIP unless the content is obviously substantive
  • Everything else → extract (see step 3)

STEP 3 — if extracting, output this structure (plain text, no JSON):

TYPE: <one of the categories above>
WHAT: <1-2 sentences describing what David is engaged with as a human
       would describe it. Not "Gmail showing email from Reid" — say
       "David is reading an email from Reid about the Q3 board deck">
WHY_IT_MATTERS: <1 sentence on relevance to David's life/work/people.
                 If you can't articulate why it matters → return SKIP
                 instead of this block>
PEOPLE: <real humans involved, comma-separated; use "David" for himself.
         Use "none" if nobody identifiable.>
CONTENT:
<the substantive visible text — the actual email body, the actual
message thread, the document paragraph, the meeting agenda. Drop all
UI chrome (menus, sidebars, tabs, buttons, timestamps, unread counts,
scrollbars, filters, folder lists, app headers).>

Write as much CONTENT as needed to capture the substance (up to ~1500
chars). Brevity is only valuable when there's nothing to say.

Always write SKIP if you are unsure whether something matters.
A false-negative (SKIP real content) is recoverable — David can see
it in the source app. A false-positive (ingesting dev/admin noise)
pollutes the graph permanently."""

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
                # gpt-4.1-mini (not nano) because the classification step
                # needs reasoning — distinguishing "this is a Terminal
                # showing logs → SKIP" from "this is Superhuman showing an
                # email → extract" is worth ~$0.002 more per screenshot.
                model="gpt-4.1-mini",
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": user_message},
                ],
                max_tokens=1500,
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
