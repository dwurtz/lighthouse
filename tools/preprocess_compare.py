"""Measure the current screenshot_preprocess prompt vs a proposed new
version by running both against the same OCR text, with the historical
Gemini-vision ``cloud_desc`` from vision_shadow records as the
reference.

Steps per record:
  1. Re-OCR the .png (using the app's deja-ocr binary — same path
     production takes today).
  2. Run CURRENT preprocess (live ``_SYSTEM_PROMPT``).
  3. Run NEW preprocess (embedded below).
  4. Pair each side-by-side with the shadow record's ``cloud_desc``
     (the Gemini-vision description for the same image).

Output: /tmp/preprocess_compare.md — a markdown you can scan top-down.

No wiring is changed in the live pipeline. Merges happen only after
you approve.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from deja.screenshot_preprocess import _SYSTEM_PROMPT as CURRENT_PROMPT  # noqa: E402
from deja.screenshot_preprocess import _get_api_key  # noqa: E402


DEJA_OCR = "/Applications/Deja.app/Contents/MacOS/deja-ocr"
SHADOW_DIR = Path.home() / ".deja" / "vision_shadow"


# --- Proposed new preprocess prompt --------------------------------------
# Changes vs CURRENT_PROMPT:
#   + SALIENT_FACTS section (role@company, emails, phones, dates —
#     structured facts worth flagging at ingest time).
#   + PANES section (when multiple apps/panes are visible).
#   + Explicit wiki-linking guidance — wrap known canonical names
#     like David → [[david-wurtz]], Deja → [[deja]] when obvious.
# Everything else is verbatim to preserve behavior for most cases.

NEW_PROMPT = """You preprocess screen OCR for David Wurtz's personal knowledge graph.
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
SALIENT_FACTS: <structured facts introduced on screen that deserve to
                be remembered as standalone atoms. One per line, in
                "TYPE: value" form. Include only when present — omit
                the section entirely when nothing qualifies. Types:
                  ROLE: <Person — Title at Company>
                  EMAIL: <name — address@domain>
                  PHONE: <name — +15551234>
                  DEADLINE: <what by when>
                  DECISION: <short summary of a commitment>
                  AMOUNT: <$N or qty for — context>
                Extract facts introduced on THIS screen for the first
                time, not ambient old mentions.>
CONTENT:
<the substantive visible text — email body, message thread, document
paragraph, meeting agenda, OR for DEV_WORK: the actual terminal output,
error messages, code diffs, commands — the things that describe what
happened technically. Drop ALL UI chrome: menus, sidebars, tabs,
buttons, timestamps, unread counts, scrollbars, folder trees, app
headers, tab strips.>

Wiki-link canonical names in WHAT/WHY_IT_MATTERS when the referent is
obvious: David → [[david-wurtz]], Deja → [[deja]], Tru → [[tru]],
Blade & Rose → [[blade-and-rose]], Dominique → [[dominique-wurtz]].
Don't invent slugs for people you can't confidently identify — leave
the raw name instead.

Write as much CONTENT as needed (up to ~1500 chars). Rich content
deserves a rich summary.

Bias toward extracting rather than SKIPping when DEV_WORK is involved
— these sessions are how David's projects move forward and should be
remembered. Only SKIP when it's truly ambient (lock screen, app
switcher) or purely ephemeral (a single shell prompt, an empty Finder
window)."""


async def run_prompt(client, system_prompt: str, user_message: str) -> str:
    resp = await client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        max_tokens=1500,
        temperature=0.0,
    )
    return (resp.choices[0].message.content or "").strip()


def ocr_image(path: Path) -> str:
    try:
        out = subprocess.run(
            [DEJA_OCR, str(path)], capture_output=True, text=True, timeout=15
        )
        return (out.stdout or "").strip()
    except Exception as e:
        return f"(OCR failed: {e})"


async def process_one(client, rec_json: Path) -> dict:
    img = rec_json.with_suffix(".png")
    if not img.exists():
        return {"id": rec_json.stem, "error": "no image"}
    ref = json.loads(rec_json.read_text())
    cloud_desc = (ref.get("cloud_desc") or "").strip()

    ocr_text = ocr_image(img)
    if not ocr_text or ocr_text.startswith("(OCR failed"):
        return {"id": rec_json.stem, "error": ocr_text or "empty OCR"}

    # deja-ocr returns "[Focused: app — window]\n\n<text>". The live path
    # passes app/window separately to the preprocess prompt; extract if
    # present, otherwise leave blank.
    app, title = "", ""
    first_line, _, rest = ocr_text.partition("\n")
    if first_line.startswith("[Focused:") and "—" in first_line:
        inner = first_line[9:].rstrip("]").strip()
        app_part, _, title_part = inner.partition("—")
        app = app_part.strip()
        title = title_part.strip().strip('"')
        ocr_text = rest.strip()

    user_msg = f"App: {app}\nWindow: {title}\n\nOCR text:\n{ocr_text}"

    current, new = await asyncio.gather(
        run_prompt(client, CURRENT_PROMPT, user_msg),
        run_prompt(client, NEW_PROMPT, user_msg),
    )
    return {
        "id": rec_json.stem,
        "app": app,
        "title": title,
        "cloud_desc": cloud_desc,
        "current": current,
        "new": new,
        "ocr_chars": len(ocr_text),
    }


async def main(n: int, out_path: Path) -> None:
    recs = sorted(SHADOW_DIR.glob("*.json"))
    random.seed(13)
    picks = random.sample(recs, min(n, len(recs)))
    print(f"Picked {len(picks)} shadow records", file=sys.stderr)

    key = _get_api_key()
    if not key:
        print("No OpenAI key available — abort", file=sys.stderr)
        sys.exit(1)
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=key)

    results = []
    for i, p in enumerate(picks, 1):
        print(f"  [{i:02d}/{len(picks)}] {p.name}", file=sys.stderr, flush=True)
        r = await process_one(client, p)
        results.append(r)

    lines = ["# screenshot_preprocess A/B — current prompt vs proposed\n"]
    for i, r in enumerate(results, 1):
        lines.append(f"## {i:02d}. `{r['id']}`  ({r.get('app','?')} — {r.get('title','?')})\n")
        if r.get("error"):
            lines.append(f"*error: {r['error']}*\n")
            continue
        lines.append(f"*OCR: {r['ocr_chars']} chars*\n")
        lines.append("### cloud_desc (reference — Gemini vision on the raw image)\n")
        lines.append(f"{r['cloud_desc'] or '*(none)*'}\n")
        lines.append("### CURRENT preprocess output\n")
        lines.append("```\n" + r["current"] + "\n```\n")
        lines.append("### NEW preprocess output\n")
        lines.append("```\n" + r["new"] + "\n```\n")
        lines.append("---\n")
    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {out_path}", file=sys.stderr)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--out", type=Path, default=Path("/tmp/preprocess_compare.md"))
    args = ap.parse_args()
    asyncio.run(main(args.n, args.out))
