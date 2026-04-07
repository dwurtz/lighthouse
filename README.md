# Deja

A personal AI agent for macOS. Deja runs in the background, observes
your digital life -- messages, email, calendar, screen, clipboard, browser,
voice -- and maintains a living wiki about the people, projects, and events
that matter to you. The wiki is browsable in Obsidian, versioned in git,
and available to Claude via a Context Engine (MCP server) that gives it
persistent memory across sessions.


## Install

```bash
git clone https://github.com/dwurtz/deja.git
cd deja
./setup.sh
```

`setup.sh` checks prereqs (Python 3.10+, macOS, Node.js), creates a venv,
installs the package, installs and authenticates the `gws` CLI for Google
Workspace, prompts for your Gemini API key (stored in macOS Keychain via
`security add-generic-password`), creates your identity self-page, copies
default prompts into `~/Deja/prompts/`, and runs a health check.
Safe to re-run.

```bash
./venv/bin/python -m deja monitor   # headless CLI
```


## How it works -- the four-tier pipeline

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
(default: `gemini-2.5-flash`) generates a wiki-grounded description of
each frame. The description references `[[entity-slugs]]` from the current
wiki index so downstream tiers can link observations to pages.

Contact resolution runs at observe time: iMessage/WhatsApp phone numbers
are matched against the macOS Contacts database to resolve display names.
Consecutive messages from the same conversation are threaded.

### 2. Integrate (every 5 minutes)

`AgentLoop._analysis_cycle()` in `agent/loop.py` reads unanalyzed
observations, triages them, and runs one or two LLM calls to merge them
into the wiki.

**Triage.** Inbound message-type signals (iMessage, WhatsApp, email,
browser) are filtered through a batched `INTEGRATE_MODEL` call
(`llm/prefilter.py`) that reads the wiki index and drops noise.
Non-message signals and all outbound messages bypass triage.

**Split batches.** Kept signals are split into two batches -- messages
(iMessage, WhatsApp, email, chat, microphone) and context (screenshots,
browser, clipboard, calendar, drive, tasks). Each batch gets its own
`integrate_observations()` call so conversations get focused model
attention instead of being diluted by ambient screenshots.

**Wiki updates.** The LLM returns structured JSON: `wiki_updates` (create,
update, or delete pages), `goal_actions` (real-world operations), and
`tasks_update` (task list maintenance). Updates are applied under a shared
asyncio lock that serializes with onboarding backfill writes.

**Post-write.** After applying updates, the integrate cycle rebuilds
`index.md`, refreshes QMD search indexes, and commits all changes to git.

Model: `INTEGRATE_MODEL` (default: `gemini-2.5-flash-lite`).

### 3. Reflect (3x per day)

`run_reflection()` in `reflection.py` runs at configurable slot hours
(default: 02:00, 11:00, 18:00 local time). Uses clock-aligned scheduling,
not interval timers -- survives macOS sleep, triggers on the first agent
heartbeat past each slot boundary, runs at most once per slot.

**Context budget.** Reflect feeds the full wiki, 7 days of event pages,
up to 500KB of raw observations (~10,000 lines), the full `goals.md`,
and macOS Contacts summaries into a single LLM call. The model
(`REFLECT_MODEL`, default: `gemini-2.5-pro`) has a 1M-token context
window, so Reflect can afford to be thorough.

**Jobs:**

- Clean up the wiki: fix contradictions, merge duplicates, rewrite messy
  prose, delete stale pages, prune old event links from entity pages.
- Maintain retrieval frontmatter (`aliases`, `domains`, `keywords`) on
  every people and project page.
- Enrich contacts: before the LLM call, `people_enrichment.py` runs a
  deterministic pass that merges phone/email/company from macOS Contacts
  and Gmail headers into people page frontmatter. Never overwrites
  existing values.
- Create pages for orphan people: names mentioned in project pages with
  no people page yet, pre-enriched with contact info, are presented to
  the LLM which decides whether to create a stub.
- Track commitments from the user's outbound messages over the last 7
  days. Add new ones, flag stale ones, retract overridden ones.
- Execute `goal_actions` and `tasks_update` (same as integrate).
- Write a morning note to `reflection.md` -- what stands out, what's
  stuck, what's worth considering.

**Post-LLM.** After applying wiki updates, Reflect runs the deterministic
linkifier (`wiki_linkify.py`), refreshes QMD indexes and embeddings, and
commits everything to git.

### 4. Serve (on demand -- Context Engine)

The MCP server in `mcp_server.py` exposes the wiki to Claude Desktop and
Claude Code. No LLM calls -- pure retrieval and write-back. See the
[Context Engine](#context-engine-mcp) section below.


## The wiki

The wiki lives at `~/Deja/` (override with `DEJA_WIKI` env
var). It is an Obsidian vault, a git repo, and the agent's sole persistent
memory. Three categories of pages:

| Category | What it describes | Example slug |
|---|---|---|
| `people/` | State -- who a person is | `amanda-peffer` |
| `projects/` | State -- what a project, goal, or life thread is | `palo-alto-relocation` |
| `events/YYYY-MM-DD/` | What happened -- timestamped, entity-linked | `2026-04-05/amanda-shared-sales-data` |

Entity pages (people + projects) describe current state in clean prose.
Event pages describe what happened, with timestamps and `[[wiki-links]]`
to the entities involved. Entity pages link to recent events in a
`## Recent` section; events link back to entities in YAML frontmatter
(`people:`, `projects:`).

Root-level files:

| File | Purpose |
|---|---|
| `goals.md` | Standing context, automations, tasks, waiting-for, recurring reminders |
| `CLAUDE.md` | Wiki writing-style contract -- read by the agent every cycle |
| `reflection.md` | Morning notes from the reflect pass |
| `log.md` | Human-readable activity log (every mutation, health check, action) |
| `index.md` | Auto-generated catalog of all pages (rebuilt after every write) |
| `prompts/` | All LLM prompt templates -- edit live in Obsidian |


## Meeting Recording

Deja replaces Granola with native meeting recording. No paid
subscriptions, no third-party services -- just local audio capture,
Gemini transcription, and wiki event pages.

**How it works:**

1. One minute before a Google Calendar meeting with attendees, a
   floating pill drops from the menu bar showing the meeting title,
   time, and a **Take notes** button.
2. Click **Take notes** -- the pill expands with a scratchpad for
   jotting notes during the meeting. A separate `DejaRecorder`
   process captures system audio (both sides of the call via
   ScreenCaptureKit) and microphone (your voice via ffmpeg).
3. Audio is written as 5-minute WAV chunks to
   `~/.deja/meeting_audio/<session>/`. Chunks are transcribed
   progressively during the meeting via Gemini Flash.
4. Click **Stop** (or 5 minutes of silence triggers auto-stop) --
   the pill shows "Generating notes..." while Gemini Pro creates the
   event page: AI-generated title, summary with key decisions and
   action items, your scratchpad notes (verbatim), and a cleaned-up
   transcript with speaker labels in a collapsible block.
5. Obsidian opens the event page automatically.

**Works without a calendar event too.** Click **Take notes** in the
menu bar header anytime -- phone calls, in-person conversations,
anything. The AI generates the title from the transcript content.

Files: `menubar/DejaRecorder.swift` (ScreenCaptureKit audio
capture), `meeting_transcribe.py` (transcription + wiki page
creation), `meeting_coordinator.py` (calendar-aware pill trigger).


## Goals

`goals.md` is the bridge between the user's intent and the agent's
behavior. It has five sections:

| Section | Managed by | Purpose |
|---|---|---|
| **Standing context** | User | Facts the agent should always know (schedules, relationships, priorities) |
| **Automations** | User | Trigger-action rules the agent executes (e.g., "TeamSnap email -> calendar event") |
| **Tasks** | Agent | Commitments the user made, extracted from outbound messages |
| **Waiting for** | Agent | Things other people owe the user, extracted from inbound promises |
| **Recurring** | User | Periodic reminders the agent surfaces in morning notes |

The agent reads all five sections every cycle. It only writes to Tasks
and Waiting For via `tasks_update` operations returned by the LLM:

- `add_tasks` -- new commitment observed in an outbound message
- `complete_tasks` -- evidence the user completed something
- `add_waiting` -- someone promised the user something
- `resolve_waiting` -- the promised thing arrived

Implementation: `goals.py` / `apply_tasks_update()`.

### goal_actions

When the LLM sees observations that match a user-defined automation in
`goals.md`, it can emit `goal_actions` -- real-world operations the agent
executes immediately. Six action types, implemented in `goal_actions.py`:

| Action type | What it does | Safety |
|---|---|---|
| `calendar_create` | Creates a Google Calendar event via `gws` | Self-addressed |
| `calendar_update` | Updates an existing event by ID | Self-addressed |
| `draft_email` | Creates a Gmail **draft** (never sends) | User reviews before sending |
| `create_task` | Adds to Google Tasks | Self-addressed |
| `complete_task` | Marks a task done by ID | Self-addressed |
| `notify` | macOS notification banner via `osascript` | Read-only |

Actions only fire when `goals.md` explicitly defines the automation. Every
action is logged to `log.md`.


## Context Engine (MCP)

Claude has no memory between sessions. The Context Engine gives it one.
It runs as an MCP server (`python -m deja mcp`) and exposes three
tools:

| Tool | What it does |
|---|---|
| `get_context(topic)` | Synthesizes a bundle of relevant wiki pages, the user profile, one-hop linked entities, and recent observations mentioning the topic. Uses QMD hybrid search (BM25 + vector + HyDE reranking) across the entire wiki including events. One call replaces 5-6 manual lookups. |
| `update_wiki(action, category, slug, content, reason)` | Write or delete a page. Git-committed and reversible. |
| `recent_activity(minutes, source)` | Raw observation stream from the last N minutes, optionally filtered by source. |

**System instruction.** The MCP server injects an instruction into every
Claude session telling it to always call `get_context` before answering
questions about people, projects, commitments, or recent events.

**Claude Desktop config:**

```json
{
  "mcpServers": {
    "deja": {
      "command": "/path/to/venv/bin/python",
      "args": ["-m", "deja", "mcp"]
    }
  }
}
```

File: `~/Library/Application Support/Claude/claude_desktop_config.json`


## LLM routing

| Tier | Model (config key) | Default | Why |
|---|---|---|---|
| Observe/screen | `VISION_MODEL` | `gemini-2.5-flash` | Best wiki-link grounding in vision eval (15/15 vs Flash-Lite); 4x more entity refs than Flash-Lite at 1/4 Pro cost |
| Prefilter | `INTEGRATE_MODEL` | `gemini-2.5-flash-lite` | Noise filter -- recall-biased, batched, cheap |
| Integrate | `INTEGRATE_MODEL` | `gemini-2.5-flash-lite` | Precision for event creation + entity attribution, every 5 min |
| Reflect | `REFLECT_MODEL` | `gemini-2.5-pro` | Deepest reasoning, full wiki context, 3 calls/day |
| Onboard | `REFLECT_MODEL` | `gemini-2.5-pro` | One-time high-stakes bootstrap from historical data |
| Chat | `REFLECT_MODEL` | `gemini-2.5-pro` | Tool use in conversational context |
| MCP | -- | none | Pure retrieval, no LLM |

All models are Gemini. The API key is resolved via `secrets.py`:
`GEMINI_API_KEY` env var > `GOOGLE_API_KEY` env var > macOS Keychain
(service: `deja`, account: `gemini-api-key`). Falls back to legacy
keychain service `lighthouse` for migration.


## CLI

| Command | What it does |
|---|---|
| `deja configure` | Interactive first-run setup -- API key, self-page, prompts, health check |
| `deja health` | Print all startup probes + current config state |
| `deja monitor` | Headless observe + integrate + reflect loop |
| `deja web [--port N]` | FastAPI backend for the menu-bar popover (default port 5055) |
| `deja mcp` | Start the Context Engine MCP server (stdio transport) |
| `deja status` | Print last observation timestamp + liveness |
| `deja linkify [--dry-run]` | Sweep the wiki and wrap unlinked entity mentions in `[[slug]]` syntax |
| `deja onboard [--days N] [--only SOURCE] [--force]` | One-time wiki bootstrap from historical email, iMessage, WhatsApp, calendar, Meet transcripts |

Source: `__main__.py`. The menu-bar app (`Deja.app`) spawns
`monitor` and `web` as child processes.


## Data locations

| Path | Contents |
|---|---|
| `~/.deja/` | Runtime state: `observations.jsonl`, `integrations.jsonl`, `config.yaml`, `deja.log`, `conversation.json`, `last_reflection_run`, PID files |
| `~/Deja/` | The wiki (git repo). Override via `DEJA_WIKI` env var. |
| macOS Keychain | Gemini API key (service: `deja`, account: `gemini-api-key`) |


## Configuration

`~/.deja/config.yaml` -- everything optional, sensible defaults:

```yaml
# LLM routing
integrate_model: gemini-2.5-flash-lite   # prefilter + integrate
vision_model:    gemini-2.5-flash         # screen descriptions
reflect_model:   gemini-2.5-pro           # reflect + chat + onboard

# Reflection schedule (local hours, 0-23)
reflect_slot_hours: [2, 11, 18]

# Observation cadence (seconds)
observe_interval: 3
integrate_interval: 300

# Screenshot kill switch
screenshot_enabled: true

# Apps the screen-description collector skips
ignored_apps: [cmux, Activity Monitor, Python, Terminal]

# Identity -- which people/*.md page is the user (usually auto-detected
# from `self: true` in frontmatter)
# user_slug: jane-doe
```


## macOS permissions

Grant each in **System Settings > Privacy & Security**. Run
`deja health` to see which are still missing.

| Permission | Why |
|---|---|
| **Full Disk Access** (Deja.app + Python binary) | iMessage and WhatsApp SQLite database reads |
| **Screen & System Audio Recording** (Deja.app) | Screenshot observations via `screencapture` |
| **Contacts** (Deja.app) | macOS Contacts enrichment in reflect pass |
| **Microphone** (Deja.app) | Push-to-record Listen button in the popover |


## Privacy

Everything runs on your Mac. The only network traffic is Gemini API calls
carrying observation and wiki context for that specific LLM request. No
telemetry, no analytics, no third parties. The wiki is a local git repo --
every change is versioned and reversible. The API key lives only in your
macOS Keychain, never written to any file, never committed to git.


## Tests

~156 unit tests across 14 files, no network required. Run with
`./venv/bin/python -m pytest tests/`.


## License

TBD.
