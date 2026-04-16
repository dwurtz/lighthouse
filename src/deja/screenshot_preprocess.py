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

# System prompt — classify the app/window by CATEGORY, extract the
# substance, and label the work project if it's a dev/work session.
# The earlier "aggressively SKIP dev content" approach threw away
# legitimate signal: David IS a developer, and screenshots of his dev
# work are real signal about what he's doing. The right fix is to
# categorize properly so dev content attaches to the DEJA / TRU / etc.
# project entities and doesn't leak into his personal-life facts.
_SYSTEM_PROMPT = """You preprocess screen OCR for David Wurtz's personal knowledge graph.
David is a builder/entrepreneur in Phoenix working on Deja (this app),
Tru, Blade & Rose, and other projects. He's also a husband, father,
and recently-diagnosed heart-disease patient. The graph should remember
everything that matters to his life AND his work — including coding,
debugging, and terminal sessions, because building things IS his work.

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
    Docker, logs, debug output. Real engineering activity David is
    doing on one of his projects.
  • ADMIN_NOISE: System Settings, Spotlight, app switcher, desktop,
    dock, lock screen, empty Finder, app-launcher sheets. Pure
    ephemeral chrome with no meaning.
  • MEDIA: YouTube, Netflix, Spotify, Music, video players
  • OTHER: something that doesn't fit above

STEP 2 — decide:
  • ADMIN_NOISE → output exactly: SKIP
  • MEDIA → SKIP unless it's specific substantive media (a talk David
    is watching for research, a song he'd want to remember). Background
    playlists and algorithmic feeds → SKIP.
  • OTHER without clear substance → SKIP
  • Everything else (including DEV_WORK) → extract (see step 3)

STEP 3 — if extracting, output this structure (plain text, no JSON):

TYPE: <one of the categories above>
PROJECT: <if DEV_WORK or work-related WORK_CHAT/DOCUMENT, name the
          project being worked on (e.g., "deja", "tru", "blade-and-rose",
          "healthspan-research"). Use "personal" for non-work content.
          Use "unknown" if you genuinely can't tell.>
WHAT: <1-2 sentences describing what David is engaged with as a human
       would describe it, with wiki-links where natural. For DEV_WORK,
       describe the ACTIVITY and SUBJECT, not the text verbatim. E.g.,
       "[[david-wurtz]] is debugging the graphiti ingest worker in
       [[deja]] — has just diagnosed an OpenAI quota error and is
       about to add billing credits." NOT: "Terminal shows 429 error,
       worker restart log, curl commands."
WHY_IT_MATTERS: <1 sentence on relevance. For DEV_WORK: what problem
                 is being solved or what progress is being made on which
                 project. For PERSONAL/EMAIL: who it involves and why
                 it's meaningful. If truly nothing matters → return SKIP.>
PANES: <only when the screen shows MULTIPLE distinct apps/windows/panes
        side-by-side. One short sentence per pane. Skip this section
        entirely when there's a single active view.>
PEOPLE: <real humans involved; use "David" for himself, "none" if
         nobody else identifiable. For DEV_WORK it's fine if this is
         just "David" or includes AI tools like "Claude".>
SALIENT_FACTS: <structured facts visible on screen that a good
                assistant would jot down for later. One per line, in
                "TYPE: value" form. Omit the section entirely when
                nothing qualifies. Extract liberally — err toward
                capturing facts, not toward filtering. Types:
                  ROLE: <Person — Title at Company>
                  CONTACT: <Person — email or phone>
                  EMAIL: <address@domain — whose it is>
                  PHONE: <+15551234 — whose it is>
                  APPOINTMENT: <what — when — where>
                  PRESCRIPTION: <drug — source / pharmacy / prescriber>
                  DEADLINE: <what — by when>
                  DECISION: <short summary of a commitment made>
                  AMOUNT: <$N or qty — for / context>
                  URL: <link — what it is>
                Include a fact whenever its value is visible on this
                screen and would be worth remembering, regardless of
                whether you've seen it before.>
CONTENT:
<the substantive visible text — email body, message thread, document
paragraph, meeting agenda, OR for DEV_WORK: the actual terminal output,
error messages, code diffs, commands — the things that describe what
happened technically. Drop ALL UI chrome: menus, sidebars, tabs,
buttons, timestamps, unread counts, scrollbars, folder trees, app
headers, tab strips.>

Wiki-linking is IMPORTANT — do it aggressively and everywhere. For
every person / project name you mention in WHAT, WHY_IT_MATTERS, or
SALIENT_FACTS, scan the ``KNOWN WIKI SLUGS`` list in the user message
and wrap the name as ``[[slug]]`` when a slug's title/summary matches
the entity on screen.

Example: if the slug block contains
  - [[joan-levinson]] — Joan Levinson is a real estate agent...
  - [[miles-wurtz]] — Miles is David's son...
and the OCR says "Email from Joan Levinson" and mentions Miles's
gymnastics pickup, emit "[[joan-levinson]] sent an email" and
"[[miles-wurtz]]'s gymnastics pickup" — NOT "Joan Levinson" or "Miles".

When no slug in the list matches, leave the raw name. Never invent a
slug. Events (date-prefixed pages) are not in the list and should not
be linked.

Write as much CONTENT as needed (up to ~1500 chars). Rich content
deserves a rich summary.

Bias toward extracting rather than SKIPping when DEV_WORK is involved
— these sessions are how David's projects move forward and should be
remembered. Only SKIP when it's truly ambient (lock screen, app
switcher) or purely ephemeral (a single shell prompt, an empty Finder
window)."""

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


# How many of index.md's top-of-file entries to surface to the
# preprocess LLM. index.md is auto-generated, ordered most-recently-
# touched first, and each line is "- [[slug]] — one-line summary". 50
# lines ≈ 10KB ≈ 2.5K input tokens on gpt-4.1-mini — cheap enough to
# pay per screenshot and rich enough that the slug list disambiguates
# from the summary (e.g. "[[rob-hurst]] — New Patient Advisor at
# HealthspanMD" vs "[[robert-toy]] — Coach Rob, gymnastics").
_CANDIDATE_SLUG_LINES = 50


def _candidate_slugs_block() -> str:
    """Read the top N slug lines from ~/Deja/index.md.

    index.md is an auto-rebuilt recency-ordered catalog of every
    people/ + projects/ page; the header comment explicitly calls out
    "LLM consumers (vision prompt, triage prefilter, integrate
    retrieval) read this top-down" — preprocess joins that club.

    Returns the raw slug-summary lines joined with newlines so the
    prompt keeps the summaries (they disambiguate collisions like
    the two "Rob"s). Empty string on any read failure — the prompt
    degrades to "no known slugs" and leaves raw names intact.
    """
    try:
        from deja.config import WIKI_DIR
    except Exception:
        return ""
    index_path = WIKI_DIR / "index.md"
    if not index_path.is_file():
        return ""
    try:
        lines: list[str] = []
        with index_path.open(encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if not s.startswith("- [["):
                    continue
                lines.append(s)
                if len(lines) >= _CANDIDATE_SLUG_LINES:
                    break
        return "\n".join(lines)
    except OSError:
        log.debug("candidate_slugs: failed to read %s", index_path, exc_info=True)
        return ""


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

    slug_lines = _candidate_slugs_block()
    known_slugs_block = (
        "KNOWN WIKI SLUGS (most-recently-touched first — the ONLY valid "
        "link targets; never invent a slug):\n" + slug_lines
        if slug_lines
        else "KNOWN WIKI SLUGS: (none — leave names as raw text)"
    )
    user_message = (
        f"App: {app_name}\n"
        f"Window: {window_title}\n\n"
        f"{known_slugs_block}\n\n"
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
