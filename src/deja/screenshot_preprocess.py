"""Preprocess raw screen OCR into a compact signal for integrate.

Raw screenshots produce 3000-5000 chars of OCR including UI chrome,
sidebars, menu bars — most of it noise. This module runs a cheap
Gemini Flash-Lite call to extract only the substantive content: what
the user is doing, who's involved, and the key visible text.

Output is either:
- A structured compact summary (~500-2500 chars) ready for integrate
- None when the content is pure chrome and should be dropped (SKIP)

Uses the same Gemini proxy as every other LLM path in Deja — no
second billing surface, no OpenAI key to manage. Flash-Lite pricing:
$0.10/M input, $0.40/M output (roughly $0.0003 per screenshot).
"""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)

# Model for the preprocess step. Flash-Lite is the cheap+fast tier —
# used elsewhere for dedup confirmation and the command-center
# classifier. If preprocess quality degrades, bump to gemini-2.5-flash.
_PREPROCESS_MODEL = "gemini-2.5-flash-lite"

# System prompt — classify the app/window by CATEGORY, extract the
# substance, and label the work project if it's a dev/work session.
# The earlier "aggressively SKIP dev content" approach threw away
# legitimate signal: many users ARE developers, and screenshots of
# their dev work are real signal. The right fix is to categorize
# properly so dev content attaches to project entities and doesn't
# leak into personal-life facts.
_SYSTEM_PROMPT = """You preprocess screen OCR for the user's personal knowledge graph.
The graph should remember everything that matters to their life AND
their work — including coding, debugging, and terminal sessions,
because building things IS work for many users.

You are given the app name, window title, and OCR text. Reason carefully.

STEP 1 — classify what's on screen (one of):
  • PERSONAL_COMM: Messages, WhatsApp, Signal, iMessage, FaceTime
  • EMAIL: Superhuman, Gmail, Mail.app, Outlook
  • DOCUMENT: Google Docs, Notion, Notes, Obsidian, Word, Pages
  • CALENDAR_PLANNING: Calendar, Linear, Things, Todoist, Asana
  • WEB_CONTENT: Safari, Chrome, Arc — article, tweet, video, product page
  • WORK_CHAT: Slack, Discord, Teams — substantive work conversation
  • MEETING: Zoom, Meet, FaceTime active meeting
  • DEV_WORK: Terminal, iTerm, VS Code, Xcode, Claude Code, Console,
    Docker, logs, debug output. Real engineering activity the user
    is doing on one of their projects.
  • ADMIN_NOISE: System Settings, Spotlight, app switcher, desktop,
    dock, lock screen, empty Finder, app-launcher sheets. Pure
    ephemeral chrome with no meaning.
  • MEDIA: YouTube, Netflix, Spotify, Music, video players
  • OTHER: something that doesn't fit above

STEP 2 — decide:
  • ADMIN_NOISE → output exactly: SKIP
  • MEDIA → SKIP unless it's specific substantive media (a talk the
    user is watching for research, a song they'd want to remember).
    Background playlists and algorithmic feeds → SKIP.
  • OTHER without clear substance → SKIP
  • Everything else (including DEV_WORK) → extract (see step 3)

STEP 3 — if extracting, output this structure (plain text, no JSON):

TYPE: <one of the categories above>
WHAT: <1-2 sentences describing what the user is engaged with as a
       human would describe it. For DEV_WORK, describe the ACTIVITY
       and SUBJECT, not the text verbatim. E.g., "Debugging an
       ingest worker in <project> — has just diagnosed a quota
       error and is about to add billing credits." NOT: "Terminal
       shows 429 error, worker restart log, curl commands."
WHY_IT_MATTERS: <1 sentence on relevance. For DEV_WORK: what problem
                 is being solved or what progress is being made on which
                 project. For PERSONAL/EMAIL: who it involves and why
                 it's meaningful. If truly nothing matters → return SKIP.>
PANES: <only when the screen shows MULTIPLE distinct apps/windows/panes
        side-by-side. One short sentence per pane. Skip this section
        entirely when there's a single active view.>
PEOPLE: <real humans involved; use "user" for the user themselves,
         "none" if nobody else identifiable. For DEV_WORK it's fine
         if this is just "user" or includes AI tools like "Claude".>
SALIENT_FACTS: <structured facts visible on screen that a good
                assistant would jot down for later. One per line, in
                "TYPE: value" form. Omit the section entirely when
                nothing qualifies. Extract liberally — err toward
                capturing facts, not toward filtering. Types:
                  ROLE: <Person — Title at Company>
                  CONTACT: <Person — email or phone>
                  EMAIL: <address@domain — whose it is>
                  PHONE: <+15551234 — whose it is>
                  PRESCRIPTION: <drug — source / pharmacy / prescriber>
                  DEADLINE: <what — by when>
                  DECISION: <short summary of a commitment made>
                  AMOUNT: <$N or qty — for / context>
                  URL: <link — what it is>
                Include a fact whenever its value is visible on this
                screen and would be worth remembering, regardless of
                whether you've seen it before.
                DO NOT emit APPOINTMENT entries. Calendar events come
                from the Google Calendar API as structured signals;
                inferring appointment times from visual calendar grids
                or email notification previews has produced
                hallucinated cell-to-time mappings (e.g. reading a
                12pm cell as 4:30pm) and fabricated attendee pages
                from garbled names. If the user's calendar is on
                screen, it's [T3] context — not a source of schedule
                facts.>
CONTENT:
<the substantive visible text — email body, message thread, document
paragraph, meeting agenda, OR for DEV_WORK: the actual terminal output,
error messages, code diffs, commands — the things that describe what
happened technically. Drop ALL UI chrome: menus, sidebars, tabs,
buttons, timestamps, unread counts, scrollbars, folder trees, app
headers, tab strips.>

Write as much CONTENT as needed (up to ~1500 chars). Rich content
deserves a rich summary. Do NOT wiki-link names yourself — integrate
resolves `[[slug]]` references downstream with full wiki context. Use
plain names here.

Bias toward extracting rather than SKIPping when DEV_WORK is involved
— these sessions are how the user's projects move forward and should
be remembered. Only SKIP when it's truly ambient (lock screen, app
switcher) or purely ephemeral (a single shell prompt, an empty Finder
window)."""

# Reuse the client across calls — building the httpx AsyncClient is
# cheap but repeated module-level construction is pointless.
_client = None


def _get_client():
    """Lazy-build a GeminiClient (proxy-routed)."""
    global _client
    if _client is not None:
        return _client
    try:
        from deja.llm_client import GeminiClient
        _client = GeminiClient()
        return _client
    except Exception:
        log.warning(
            "screenshot_preprocess: failed to construct GeminiClient — "
            "preprocessing disabled, raw OCR will flow through",
            exc_info=True,
        )
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
        # Proxy unavailable — degrade to raw OCR so integrate still gets
        # something rather than dropping the signal entirely.
        return ocr_text

    # Gemini's _generate takes one `contents` blob — system and user
    # text are concatenated into one prompt (same shape every other
    # integrate/dedup call in this codebase uses).
    full_prompt = (
        _SYSTEM_PROMPT
        + "\n\n"
        + f"App: {app_name}\n"
        + f"Window: {window_title}\n\n"
        + f"OCR text:\n{ocr_text}"
    )

    try:
        content = await asyncio.wait_for(
            client._generate(
                model=_PREPROCESS_MODEL,
                contents=full_prompt,
                config_dict={
                    "max_output_tokens": 1500,
                    "temperature": 0.0,
                },
            ),
            timeout=15.0,
        )
        content = (content or "").strip()
    except asyncio.TimeoutError:
        log.warning("screenshot_preprocess: timeout, falling back to raw OCR")
        return ocr_text
    except Exception:
        log.warning(
            "screenshot_preprocess: API call failed, falling back to raw OCR",
            exc_info=True,
        )
        return ocr_text

    if not content:
        log.warning("screenshot_preprocess: empty content, falling back to raw OCR")
        return ocr_text

    # Flash-Lite sometimes echoes the prompt's step numbering before
    # emitting the schema. Strip anything above the first TYPE: line
    # so integrate sees only the structured block. gpt-4.1-mini never
    # did this; the extra defensiveness is cheap.
    if "TYPE:" in content:
        content = content[content.index("TYPE:") :].strip()

    # Treat any response that is exactly SKIP (or trivially so) as a skip
    # sentinel. Be tolerant: the model occasionally wraps it in quotes or
    # adds a trailing period.
    stripped = content.strip().strip('"').strip("'").rstrip(".").strip()
    if stripped.upper() == "SKIP":
        return None

    return content
