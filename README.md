# Lighthouse

A personal AI agent that sits in your menu bar, watches what you do on
your Mac, and maintains a living wiki about the people and projects
that matter to you. You read the wiki in Obsidian. You correct it by
chatting with the agent. You never file a ticket against yourself again.

## Install

```bash
git clone https://github.com/dwurtz/lighthouse.git
cd lighthouse
./setup.sh
```

That's it. The setup script walks you through everything interactively:

1. **Checks prereqs** — Python 3.10+, macOS, optional `ffmpeg` + `gws`
2. **Creates a venv** and installs the package
3. **Prompts for your Gemini API key** and stores it in your macOS
   Keychain (not in `.env`, not in `.zshrc`, not in any file on disk)
4. **Creates your identity self-page** — name, email, preferred name
5. **Installs default prompts** into your wiki at `~/Lighthouse/prompts/`
6. **Runs a health check** so you see immediately if anything's wrong

After that, start the agent:

```bash
./venv/bin/python -m lighthouse monitor   # headless CLI
# or
open Lighthouse.app                        # menu bar app (build instructions below)
```

## What it does

Three cadences running in the background:

- **Observe** (every few seconds) — captures raw context from iMessage,
  WhatsApp, Gmail, Google Calendar / Drive / Tasks, Chrome history,
  screen contents, clipboard, active window, microphone, and in-app chat.
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
calls to the Gemini API (which you can review per-request in
`~/.lighthouse/lighthouse.log`).

## Prerequisites

**Required:**

- macOS 14 (Sonoma) or later
- Python 3.10+
- A free Gemini API key from [Google AI Studio](https://aistudio.google.com/app/apikey)

**Optional (but recommended):**

- [`ffmpeg`](https://ffmpeg.org/) for the push-to-record microphone button
  in the popover — `brew install ffmpeg`
- [`gws`](https://github.com/dwurtz/gws) CLI authenticated as your Google
  Workspace user, for Gmail / Calendar / Drive / Tasks observations

## CLI

| Command | What it does |
|---|---|
| `lighthouse configure` | Interactive first-run setup — safe to re-run |
| `lighthouse health` | Print all startup checks + current config state |
| `lighthouse monitor` | Headless observe + integrate + reflect loop |
| `lighthouse web` | FastAPI backend for the menu-bar popover |
| `lighthouse status` | Print last observation timestamp + liveness |
| `lighthouse linkify [--dry-run]` | Sweep the wiki and wrap unlinked entity mentions in `[[slug]]` |
| `lighthouse onboard [--days N]` | One-time wiki bootstrap from the last N days of sent email + iMessage + WhatsApp |

The menu bar app (`Lighthouse.app`) spawns `monitor` and `web` as child
processes automatically. The CLI subcommands are for ops, debugging,
and scripted operations.

## Data locations

| Path | Contents |
|---|---|
| `~/.lighthouse/` | Runtime state: `observations.jsonl`, `integrations.jsonl`, `lighthouse.log`, `conversation.json`, `last_reflection_run`, `config.yaml` |
| `~/Lighthouse/` | Your wiki (git repo). Override via the `LIGHTHOUSE_WIKI` env var. |
| `~/Lighthouse/people/` | One markdown page per real person |
| `~/Lighthouse/projects/` | One markdown page per active project, goal, or life thread |
| `~/Lighthouse/prompts/` | All five LLM prompts — edit live in Obsidian |
| `~/Lighthouse/CLAUDE.md` | The writing-style contract the agent reads every cycle |
| `~/Lighthouse/reflection.md` | Morning notes from the reflect pass |
| `~/Lighthouse/log.md` | Human-readable activity log (every wiki mutation, every health check, every chat turn) |

API keys live in the **macOS Keychain** (service: `lighthouse`, account:
`gemini-api-key`), not in any file on disk.

## Configuration

`~/.lighthouse/config.yaml` — everything optional, sensible defaults:

```yaml
# LLM routing
integrate_model: gemini-2.5-flash-lite   # text-only integrate + prefilter
vision_model:    gemini-2.5-flash        # screen descriptions
reflect_model:   gemini-2.5-pro          # daily wiki reflection + chat

# Reflection schedule (local hours, 0-23)
reflect_slot_hours: [2, 11, 18]

# Observation cadence (seconds)
observe_interval: 3
integrate_interval: 300

# Kill switch for screen capture — set to false if you don't want the
# agent taking screenshots (macOS will also require you to grant Screen
# Recording permission before any capture can happen either way)
screenshot_enabled: true

# Apps the screen-description collector should skip
ignored_apps: [cmux, Activity Monitor, Python, Terminal]
```

## macOS permissions

After first launch, Lighthouse needs a handful of macOS Privacy &
Security grants. The `lighthouse health` output tells you which are
still missing. Grant each in **System Settings → Privacy & Security**:

| Permission | Why |
|---|---|
| **Full Disk Access** → Lighthouse.app + your Python binary | iMessage + WhatsApp SQLite reads |
| **Contacts** → Lighthouse.app | macOS Contacts enrichment |
| **Screen & System Audio Recording** → Lighthouse.app | Screenshot observations |
| **Microphone** → Lighthouse.app | Push-to-record Listen button |

Re-run `lighthouse health` after each grant to confirm.

## Building the menu bar app

```bash
cd menubar
swiftc Lighthouse.swift -parse-as-library -o Lighthouse \
  -framework Cocoa -framework SwiftUI -framework AVFoundation \
  -target arm64-apple-macos14

# Assemble into an .app bundle
mkdir -p ../Lighthouse.app/Contents/MacOS
cp Lighthouse ../Lighthouse.app/Contents/MacOS/
# Copy an Info.plist from your fork or write one (CFBundleExecutable=Lighthouse, CFBundleIdentifier=com.lighthouse.app)

# Ad-hoc sign so macOS TCC can track it
codesign --force --deep --sign - ../Lighthouse.app

open ../Lighthouse.app
```

The menu bar icon is loaded from `~/.lighthouse/icon.png` at runtime —
drop any monochrome 22×22 PNG there and restart the app to swap it.
Falls back to SF Symbols (`rays`) if the file is missing.

## Tests

```bash
./venv/bin/python -m pytest tests/
```

~196 unit tests, ~1s runtime, no network. Plus opt-in live tests
(`pytest -m vision`) that call Gemini and cost a few pennies per run,
and a standalone A/B harness at `tools/vision_eval.py` for comparing
vision models on real frames.

## More

- **`~/Lighthouse/HOW_IT_WORKS.md`** — full architecture walkthrough in
  the wiki: three-tier loop, observation sources, LLM stack, retrieval,
  chat tools, identity, operational runbook, privacy posture.
- **`~/Lighthouse/CLAUDE.md`** — the wiki-writing contract (read by the
  agent on every cycle). Edit this to shape how your wiki reads.
- **`~/Lighthouse/reflection.md`** — morning notes from the reflect
  pass. Read daily.

## Privacy

Everything happens on your Mac. The only network traffic is LLM API
calls to Google's Gemini endpoint, which carry the observation and wiki
context for that specific call. No telemetry, no analytics, no third
parties. Your wiki is a local git repo so every change is versioned
and reversible via `git revert`.

The API key lives only in your macOS Keychain. It's never written to
any file, never committed to git, never inherited into subprocess
environments that don't need it. You can rotate it any time via
`lighthouse configure`.

## License

TBD.
