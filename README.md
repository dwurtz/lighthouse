# Deja

A personal AI agent for macOS. Deja runs in the background, observes
your digital life — messages, email, calendar, screen, clipboard, browser,
voice — and maintains a living wiki about the people, projects, and events
that matter to you. The wiki is browsable in Obsidian, versioned in git,
and available to Claude via an MCP server that gives it persistent
memory across sessions.


## Install

### Quick install (recommended)

One-line curl-bash for personal Macs:

```bash
curl -fsSL https://raw.githubusercontent.com/dwurtz/deja/main/install.sh | bash
```

The installer checks prereqs, downloads the latest signed DMG from
GitHub Releases, copies `Deja.app` to `/Applications`, strips the
quarantine flag, and launches. Re-run the same command later to
update — it's idempotent. See [INSTALL.md](INSTALL.md) for the full
walkthrough.

**Prereqs the installer enforces:**
- Apple Silicon Mac (M1 or newer)
- macOS 14 (Sonoma) or newer
- [Claude Code](https://claude.com/code) on `PATH` — required for the
  integrate cycle, which runs Claude Opus via a `claude -p` subprocess.
  Run `claude /login` if you haven't already.

### Manual DMG install

If you'd rather not curl-bash:

1. Download the DMG from the [latest release](https://github.com/dwurtz/deja/releases/latest)
   or [trydeja.com/download](https://trydeja.com/download).
2. Mount the DMG, drag `Deja.app` to `/Applications`.
3. Strip the macOS quarantine flag so Gatekeeper doesn't show a
   "damaged" warning (the build is ad-hoc signed, not Apple-notarized):
   ```bash
   xattr -dr com.apple.quarantine /Applications/Deja.app
   ```
4. Launch Deja from `/Applications`.

### First-launch setup

Deja runs a guided wizard the first time it opens:

1. Verifies `claude` is on `PATH`.
2. Walks Google Workspace OAuth (Gmail, Calendar, Drive read).
3. Creates your identity self-page in the wiki.
4. Auto-configures itself as an MCP server on Claude Desktop, Claude
   Code, Cursor, Windsurf, and any other detected AI clients.
5. Runs a health check and surfaces any missing macOS permissions.
6. Kicks off a one-time backfill of the last 30 days of sent email,
   iMessage, WhatsApp, calendar, and Meet transcripts to bootstrap
   the wiki.

Prompt templates ship inside the app bundle and update with each
Sparkle release — no per-user copy in the wiki.

### Developer install (from source)

For modifying or contributing to Deja. Full walkthrough in
[INSTALL.md](INSTALL.md#building-from-source); short version:

```bash
git clone https://github.com/dwurtz/deja.git
cd deja
python3.14 -m venv venv && ./venv/bin/pip install -e .
bash menubar/bundle-python.sh
make dev
```

Source builds keep TCC permissions stable across rebuilds because the
local code signature is consistent for you. No Apple Developer ID
needed.


## How it works — the four-tier pipeline

Deja has four tiers that run at different cadences, using different
models, at different costs. Together they form a pipeline: observe raw
context, integrate it into structured wiki pages, reflect on the full
picture periodically, and serve the result to Claude on demand.

### 1. Observe (every 3 seconds)

The `Observer` class in `observations/collector.py` polls every source and
appends new entries to `observations.jsonl`. Sources:

| Source | Module | Cadence |
|---|---|---|
| iMessage | `observations/imessage.py` | every cycle (3s) |
| WhatsApp | `observations/whatsapp.py` | every cycle |
| Clipboard | `observations/clipboard.py` | every cycle |
| Screenshot | `observations/screenshot.py` | every cycle (on app change or every 6s) |
| Browser history | `observations/browser.py` | every 3rd cycle (~9s) |
| Gmail | `observations/email.py` | every 5th cycle (~15s) |
| Google Calendar | `observations/calendar.py` | every 5th cycle |
| Google Drive | `observations/drive.py` | every 5th cycle |
| Google Tasks | `observations/tasks.py` | every 5th cycle |
| Granola notes | `observations/granola.py` | every 5th cycle |
| Meet transcripts | `observations/meet.py` | every 5th cycle |
| Meeting recording | `meeting_transcribe.py` + `DejaRecorder` | user-initiated |

No LLM calls happen at this tier except for screenshots: `VISION_MODEL`
(default: `gemini-2.5-flash`) generates a wiki-grounded OCR description
of each frame for OCR-only consumer paths. The integrate path itself
does not consume this preprocessed text — it reads raw screenshot
pixels directly via Claude vision (see Integrate, below).

Contact resolution runs at observe time: iMessage/WhatsApp phone numbers
are matched against the macOS Contacts database to resolve display names.
Consecutive messages from the same conversation are threaded.

### 2. Integrate (every 5 minutes)

`AgentLoop._analysis_cycle()` in `agent/loop.py` reads unanalyzed
observations, runs deterministic rule-based triage to drop noise, and
calls `claude -p` with the integrate prompt plus the cycle's screenshot
PNGs as multimodal content blocks. Pure pixels in, JSON out.

The LLM returns structured JSON: `wiki_updates` (create, update, or
delete pages), `goal_actions` (real-world operations), and
`tasks_update` (task list maintenance). Updates are applied under a
shared asyncio lock, then `index.md` is rebuilt, QMD search indexes
refreshed, and everything is committed to git.

Model: `claude-opus-4-7` via `integrate_claude_vision.py` — hardcoded,
no config key.

### 3. Reflect (3x per day)

`run_reflection()` in `reflection.py` runs at configurable slot hours
(default: 02:00, 11:00, 18:00 local time). Uses clock-aligned scheduling,
not interval timers — survives macOS sleep, triggers on the first agent
heartbeat past each slot boundary, runs at most once per slot.

Reflect feeds the full wiki, 7 days of event pages, up to 500KB of raw
observations (~10,000 lines), the full `goals.md`, and macOS Contacts
summaries into a single LLM call. The model (`REFLECT_MODEL`, default:
`gemini-3.1-pro-preview`) has a 1M-token context window so Reflect can
afford to be thorough.

Jobs include cleaning up the wiki (contradictions, duplicates, stale
pages), maintaining retrieval frontmatter, enriching contacts from
macOS Contacts, tracking the user's commitments from the last 7 days,
executing `goal_actions`/`tasks_update`, and writing a morning note
to `reflection.md`.

### 4. Serve — the Context Engine

`mcp_server.py` exposes the wiki and a growing surface of action tools
to Claude via MCP. Auto-configured at install time on Claude Desktop,
Claude Code, Cursor, Windsurf. See [Context Engine](#context-engine-mcp)
below.


## The wiki

The wiki lives at `~/Deja/` (override with `DEJA_WIKI` env
var). It is an Obsidian vault, a git repo, and the agent's sole persistent
memory. Three categories of pages:

| Category | What it describes | Example slug |
|---|---|---|
| `people/` | State — who a person is | `amanda-peffer` |
| `projects/` | State — what a project, goal, or life thread is | `palo-alto-relocation` |
| `events/YYYY-MM-DD/` | What happened — timestamped, entity-linked | `2026-04-05/amanda-shared-sales-data` |

Entity pages (people + projects) describe current state in clean prose.
Event pages describe what happened, with timestamps and `[[wiki-links]]`
to the entities involved. Entity pages link to recent events in a
`## Recent` section; events link back to entities in YAML frontmatter
(`people:`, `projects:`).

Root-level files:

| File | Purpose |
|---|---|
| `goals.md` | Standing context, automations, tasks, waiting-for, recurring reminders |
| `CLAUDE.md` | Wiki writing-style contract — read by the agent every cycle |
| `reflection.md` | Morning notes from the reflect pass |
| `log.md` | Human-readable activity log (every mutation, health check, action) |
| `index.md` | Auto-generated catalog of all pages (rebuilt after every write) |


## Meeting recording

Deja replaces Granola with native meeting recording: no paid
subscriptions, no third-party services — local audio capture, Gemini
transcription, and a wiki event page generated automatically.

**How it works:**

1. One minute before a Google Calendar meeting with attendees, a
   floating pill drops from the menu bar showing the meeting title,
   time, and a **Take notes** button.
2. Click **Take notes** — the pill expands with a scratchpad. A
   `DejaRecorder` subprocess captures system audio (both sides of the
   call via ScreenCaptureKit) and microphone (your voice via ffmpeg).
3. Audio is written as 5-minute WAV chunks to
   `~/.deja/meeting_audio/<session>/`. Chunks are transcribed
   progressively during the meeting.
4. Click **Stop** (or 5 minutes of silence triggers auto-stop) — the
   pill shows "Generating notes..." while the LLM creates the event
   page: AI-generated title, summary with key decisions and action
   items, your scratchpad notes (verbatim), and a cleaned-up transcript
   with speaker labels in a collapsible block.
5. Obsidian opens the event page automatically.

Click **Take notes** in the menu bar header anytime — phone calls,
in-person conversations, anything. The AI generates the title from the
transcript content.


## Goals

`goals.md` is the bridge between the user's intent and the agent's
behavior. It has five sections:

| Section | Managed by | Purpose |
|---|---|---|
| **Standing context** | User | Facts the agent should always know (schedules, relationships, priorities) |
| **Automations** | User | Trigger-action rules the agent executes (e.g., "TeamSnap email → calendar event") |
| **Tasks** | Agent | Commitments the user made, extracted from outbound messages |
| **Waiting for** | Agent | Things other people owe the user, extracted from inbound promises |
| **Recurring** | User | Periodic reminders the agent surfaces in morning notes |

The agent reads all five sections every cycle. It writes to Tasks
and Waiting For via `tasks_update` operations returned by the LLM.

### goal_actions

When the LLM sees observations that match a user-defined automation in
`goals.md`, it can emit `goal_actions` — real-world operations the agent
executes immediately (calendar events, Gmail drafts, Google Tasks
entries, macOS notifications). Self-addressed and reversible by design;
draft emails never auto-send. See `goal_actions.py`.


## Context Engine (MCP)

Claude has no memory between sessions. The Context Engine gives it one.
It runs as an MCP server (`python -m deja mcp`) and exposes a tool
surface for retrieval, write-back, search, calendar/gmail lookup,
iMessage drafting, and goal mutation. The setup wizard auto-registers
the MCP server with every detected AI client (Claude Desktop, Claude
Code, Cursor, Windsurf), so it's available the moment install
completes.

Headline tools:

| Tool | What it does |
|---|---|
| `get_context(topic)` | Synthesizes a bundle of relevant wiki pages, the user profile, one-hop linked entities, and recent observations mentioning the topic. Uses QMD hybrid search (BM25 + vector + HyDE reranking) across the entire wiki. |
| `update_wiki(action, category, slug, ...)` | Write or delete a page. Git-committed and reversible. |
| `recent_activity(minutes, source)` | Raw observation stream from the last N minutes, optionally filtered by source. |
| `gmail_search` / `gmail_get_message` | Search and read Gmail directly. |
| `calendar_list_events` | Read calendar state with date filters. |
| `draft_imessage` / `send_imessage` | Stage or send iMessage from the agent. |
| `add_task` / `add_reminder` / `add_waiting_for` | Mutate `goals.md`. |
| `browser_ask` | Drive Claude-in-Chrome for sites Deja doesn't have a direct API for. |

For the full surface, see `mcp_server.py`.


## LLM routing

| Tier | Model | Default | Why |
|---|---|---|---|
| Observe/screen | `VISION_MODEL` | `gemini-2.5-flash` | Wiki-grounded OCR description; consumed by OCR-only paths |
| Integrate | hardcoded | `claude-opus-4-7` (via `claude -p`) | Reads raw screenshot pixels directly; precision for event creation + entity attribution |
| Reflect / Onboard | `REFLECT_MODEL` | `gemini-3.1-pro-preview` | Deepest reasoning, full wiki context, 3 calls/day |
| Chat | `CHAT_MODEL` | `gemini-3.1-pro-preview` | Tool use in conversational context |
| MCP | — | none | Pure retrieval, no LLM |

The integrate cycle requires `claude` on `PATH` (Claude Code).
Gemini-backed tiers route through the Deja API server proxy by default —
no user API key required. Developers can set `GEMINI_API_KEY` (or
`GOOGLE_API_KEY`) to bypass the proxy and call Gemini directly via the
SDK.


## CLI

| Command | What it does |
|---|---|
| `deja health` | Print all startup probes + current config state |
| `deja monitor` | Headless observe + integrate + reflect loop |
| `deja web [--port N]` | FastAPI backend for the menu-bar popover (default 5055) |
| `deja mcp` | Start the Context Engine MCP server (stdio transport) |
| `deja status` | Print last observation timestamp + liveness |
| `deja linkify [--dry-run]` | Sweep the wiki and wrap unlinked entity mentions in `[[slug]]` |
| `deja onboard [--days N] [--only SOURCE] [--force]` | One-time wiki bootstrap from historical email, iMessage, WhatsApp, calendar, Meet transcripts |

Source: `__main__.py`. The menu-bar app (`Deja.app`) spawns `monitor`
and `web` as child processes.


## Data locations

| Path | Contents |
|---|---|
| `~/.deja/` | Runtime state: `observations.jsonl`, `audit.jsonl`, `config.yaml`, `deja.log`, conversation files, PID files |
| `~/Deja/` | The wiki (git repo). Override via `DEJA_WIKI` env var. |
| macOS Keychain | Optional Gemini API key (service: `deja`, account: `gemini-api-key`) — only used in developer mode bypassing the server proxy |


## Configuration

`~/.deja/config.yaml` — everything optional, sensible defaults:

```yaml
# LLM routing
# Integrate is hardcoded to claude-opus-4-7 via the `claude` CLI subprocess.
# Prefilter is deterministic (rule-based; see deja.signals.triage).
vision_model:    gemini-2.5-flash             # screen OCR descriptions
reflect_model:   gemini-3.1-pro-preview       # reflect + onboard
chat_model:      gemini-3.1-pro-preview       # chat / command

# Reflection schedule (local hours, 0-23)
reflect_slot_hours: [2, 11, 18]

# Observation cadence (seconds)
observe_interval: 3
integrate_interval: 300

# Screenshot kill switch
screenshot_enabled: true

# Apps the screen-description collector skips
ignored_apps: [cmux, Activity Monitor, Python, Terminal]

# Identity — which people/*.md page is the user (usually auto-detected
# from `self: true` in frontmatter)
# user_slug: jane-doe
```


## macOS permissions

Grant each in **System Settings → Privacy & Security**. Run
`deja health` to see which are still missing.

| Permission | Why |
|---|---|
| **Full Disk Access** (Deja.app + Python binary) | iMessage and WhatsApp SQLite database reads |
| **Screen & System Audio Recording** (Deja.app) | Screenshot observations via `screencapture` |
| **Contacts** (Deja.app) | macOS Contacts enrichment in reflect pass |
| **Microphone** (Deja.app) | Push-to-record Listen button in the popover |
| **Accessibility** (Deja.app) | Per-window focus + frontmost-app context |


## Privacy

Deja runs on your Mac. Your wiki, observations, and configuration are
local files. Network traffic consists of:

- **Claude API calls** — the integrate cycle runs Claude Opus via a
  local `claude -p` subprocess (Claude Code). Screenshots, signals, and
  retrieved wiki pages are sent to Anthropic on every cycle.
- **Gemini API calls** — vision OCR preprocess, reflection, and chat go
  to Google's Gemini models via the Deja API server proxy (or directly
  with `GEMINI_API_KEY` set in developer mode).
- **Google Workspace API calls** — Gmail, Calendar, Drive, and Tasks are
  accessed via OAuth as your Google account.

No telemetry, no analytics, no third-party services beyond Anthropic
and Google. The wiki is a local git repo — every change is versioned
and reversible.


## Tests

Run `./venv/bin/python -m pytest tests/` from a developer install.


## License

TBD.
