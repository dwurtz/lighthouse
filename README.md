# Lighthouse

A personal AI agent that sits in your menu bar, watches what you do on your
Mac, and maintains a living wiki about the people and projects that matter
to you. You read the wiki in Obsidian. You correct it by chatting with the
agent. You never file a ticket against yourself again.

## What it does

Three cadences running in the background:

- **Observe** (every few seconds) — captures raw context from iMessage,
  WhatsApp, Gmail, Google Calendar/Drive/Tasks, Chrome history, screen
  contents, clipboard, active window, microphone, and in-app chat.
- **Integrate** (every 5 minutes) — Gemini 2.5 Flash-Lite reads new
  observations, finds the wiki pages they touch via semantic retrieval,
  and merges the signal into prose. Vision (screen descriptions) runs
  on Gemini 2.5 Flash for richer grounding.
- **Reflect** (3× per day at 02:00 / 11:00 / 18:00) — Gemini 2.5 Pro
  takes a fresh pass over the whole wiki: merges duplicates, collapses
  contradictions, enriches contact info from macOS Contacts + Gmail
  headers, runs a deterministic wiki-linkifier, and writes a morning
  note to `reflection.md`.

All data stays on your Mac. The only thing leaving the machine is LLM
calls to the Gemini API.

## Quick start

**Prerequisites:**

- macOS 14 or later
- Python 3.10+
- A Gemini API key → `export GEMINI_API_KEY=...`
- Optional: `gws` CLI authenticated as your Google Workspace user (for
  Gmail / Calendar / Drive / Tasks observations and for chat contact
  enrichment)
- Optional: `ffmpeg` on `$PATH` for the push-to-record microphone button
  in the popover — `brew install ffmpeg`

**Install and run:**

```bash
python3 -m venv venv
./venv/bin/pip install -e .
./venv/bin/python -m lighthouse monitor
```

Or double-click `Lighthouse.app` — the Swift menu-bar binary spawns the
monitor + web backend as child processes. The menu bar icon is a
lighthouse; left-click opens the chat/activity popover, right-click
shows the options menu.

## First-time setup: create your self-page

Lighthouse threads your name through every Gemini prompt and uses your
email for the few outbound notifications it sends. Both come from a
**self-page** in your wiki — just a markdown file with YAML frontmatter.

Create `~/Lighthouse/people/<your-slug>.md`:

```markdown
---
self: true
emails:
  - you@example.com
phones:
  - +1-555-123-4567
preferred_name: Jane
aliases: [Jane, JD]
keywords: [product management, ai]
---

# Jane Doe

Jane is a product manager at Acme Corp living in San Francisco with her
partner and two kids. She's currently focused on shipping the Q2 redesign
and preparing for parental leave in July.
```

On next boot the startup health check confirms the self-page is found
and logs `startup check [user profile]: OK (Jane Doe <you@example.com>)`.
If it's missing, Lighthouse runs on a generic "the user" fallback and
warns you in `log.md`.

## CLI

| Command | What it does |
|---|---|
| `python -m lighthouse monitor` | Headless observe + integrate + reflect loop |
| `python -m lighthouse web` | FastAPI backend for the popover (port 5055) |
| `python -m lighthouse status` | Print last observation timestamp + liveness |
| `python -m lighthouse linkify [--dry-run]` | Sweep the wiki and wrap unlinked entity mentions in `[[slug]]` |

The Swift app spawns `monitor` and `web` automatically. The CLI is for
ops, debugging, and scripted operations.

## Data locations

| Path | Contents |
|---|---|
| `~/.lighthouse/` | Runtime state: `observations.jsonl`, `integrations.jsonl`, `lighthouse.log`, `conversation.json`, `last_reflection_run`, `config.yaml` |
| `~/Lighthouse/` | Your wiki (git repo). Override via `LIGHTHOUSE_WIKI` env var. |
| `~/Lighthouse/people/` | One markdown page per real person |
| `~/Lighthouse/projects/` | One markdown page per active project, goal, or life thread |
| `~/Lighthouse/prompts/` | All five LLM prompts (`integrate.md`, `reflect.md`, `describe_screen.md`, `prefilter.md`, `chat.md`) — edit live in Obsidian |
| `~/Lighthouse/CLAUDE.md` | The writing-style contract the agent reads on every cycle |
| `~/Lighthouse/reflection.md` | Morning notes from the reflect pass |
| `~/Lighthouse/log.md` | Human-readable activity log (every wiki mutation, every health check, every chat turn) |

Override the home dir with `LIGHTHOUSE_HOME`, the wiki dir with
`LIGHTHOUSE_WIKI`. Legacy `WORKAGENT_HOME` / `WORKAGENT_WIKI` are still
honored during migration.

## Configuration

`~/.lighthouse/config.yaml` — everything optional, sensible defaults:

```yaml
# LLM routing (see HOW_IT_WORKS.md for the rationale)
integrate_model: gemini-2.5-flash-lite   # text-only integrate + prefilter
vision_model:    gemini-2.5-flash        # screen descriptions (4x more wiki grounding than Flash-Lite in eval)
reflect_model:   gemini-2.5-pro          # daily wiki reflection + chat

# Reflection schedule (local hours, 0-23). Default 3x/day.
reflect_slot_hours: [2, 11, 18]

# Observation cadence (seconds)
observe_interval: 3
integrate_interval: 300

# Apps the screen-description collector should skip
ignored_apps: [cmux, Activity Monitor, Python, Terminal]

# Vision A/B eval — save a copy of every screenshot + its description
# to ~/.lighthouse/vision_retention/ for later offline evaluation
vision_retention: false
```

## Tests

```bash
./venv/bin/python -m pytest tests/
```

~190 unit tests, <2s, no network. Plus opt-in vision fixture tests that
hit Gemini (`pytest -m vision`) and a standalone A/B harness at
`tools/vision_eval.py` for comparing vision models on real frames.

## More

- **`~/Lighthouse/HOW_IT_WORKS.md`** — full architecture walkthrough
  in the wiki: the three-tier loop, observation sources, LLM stack,
  retrieval, chat tools, identity, operational runbook, privacy posture.
- **`~/Lighthouse/CLAUDE.md`** — the wiki-writing contract (read by
  the agent on every cycle). Edit this to shape how your wiki reads.
- **`~/Lighthouse/reflection.md`** — morning notes from the reflect
  pass. Read daily.
