# Deja — Architecture

This is the authoritative technical overview of Deja: what it is, how the pieces fit together, how data flows, and where to look when you want to change something.

Audience: a developer who's never seen the codebase and wants to understand it well enough to navigate, debug, and extend. File paths are given relative to the repo root (`/Users/wurtz/projects/deja`) and most include line refs to anchor you.

## 1. What Deja is

Deja is a personal AI chief of staff that runs on your Mac. It observes your digital life — email, iMessage, WhatsApp, screenshots, calendar, clipboard, browser, voice — and maintains a living wiki of the people, projects, and events that matter to you. On top of that wiki, a **chief of staff (cos)** agent decides when to nudge you, take action (draft a reply, create a calendar entry, close a loop), or stay silent.

Three design commitments shape everything:

- **Local-first.** Raw signals and the wiki stay on your machine. LLM calls go out to a proxy; nothing else.
- **Git-backed.** The wiki lives at `~/Deja/` as Markdown, committed after every agent write. Every change is reviewable and reversible.
- **Trust over coverage.** The failure mode to avoid is cos getting a fact wrong in an email you read on your phone. The system is designed around a disciplined filter — most cycles produce silence.

## 2. The 30-second mental model

Deja is a **two-tier system**: a Swift menubar app hosts two Python subprocesses that together drive three pipelines and two reactive agents. Everything reads and writes the same substrate — the wiki at `~/Deja/` and the state dir at `~/.deja/`.

```
                          ┌──────────────────────────────┐
                          │   /Applications/Deja.app     │
                          │   (Swift menubar, notch UI)  │
                          │                              │
                          │  ScreenCapture  HotkeyMgr    │
                          │  KeystrokeMon   VoicePill    │
                          └────────┬─────────────────────┘
                                   │ spawns + HTTP-over-unix-socket
                   ┌───────────────┴──────────────────┐
                   ▼                                  ▼
         ┌──────────────────┐               ┌──────────────────┐
         │   deja monitor   │               │    deja web      │
         │   (three loops)  │               │ (FastAPI/socket) │
         └──┬────┬──────┬───┘               └────────┬─────────┘
            │    │      │                            │ voice / setup /
            ▼    ▼      ▼                            │ command / mic
         OBSERVE INTEGRATE REFLECT                   │
          (3s)   (5min)    (3×/day)                  │
            │    │      │   deterministic prep       │
            │    │      │   + cos reflective         │
            │    │      │    ┌────────────────┐      │
            │    │      │    │ chief of staff │◄─────┤ (command mode)
            │    │      └───►│     (cos)      │      │
            │    └──────────►│  decision      │      │
            │                │  layer: fresh  │      │
            │                │  claude CLI,   │      │
            │                │  MCP attached  │      │
            │                └───────┬────────┘      │
            ▼                        ▼               ▼
         ┌─────────────────────────────────────────────────┐
         │  ~/Deja/          (wiki: people, projects,      │
         │                    events, goals.md, convos)    │
         │  ~/.deja/         (state: observations.jsonl,   │
         │                    audit.jsonl, sockets,        │
         │                    cos config, buffers)         │
         └─────────────────────────────────────────────────┘
```

Three pipelines running on different cadences, sharing the same storage:

```
OBSERVE (every 3s)
   ↓ writes to observations.jsonl
INTEGRATE (every 5min)
   ↓ reads new signals + retrieves wiki context → LLM → writes wiki pages,
   ↓ event pages, goal mutations; emits observation_narrative
REFLECT (3×/day, 02/11/18 local)
   ↓ thin prep (QMD vector-index refresh) → single cos reflective pass.
   ↓ Cos invokes candidate-generator MCP tools (dedup pairs,
   ↓ orphan-event clusters, open-loop evidence, contradiction pairs)
   ↓ on demand, judges each candidate, writes via the usual MCP tools.
```

**Cos is the decision layer.** Everything else — the signal tiering, the Flash preprocess, vector similarity, clustering, candidate generators — is cheap analyst work preparing material for cos. The integrate LLM still writes the wiki directly (it's an immediate classifier over a bounded signal batch), but reflect's old Flash-confirm sweeps are gone: cos itself now makes every judgment call in reflect, using its MCP tool surface to verify and, when necessary, asking the user via `send_email_to_self`.

Plus two reactive layers on top:

- **Chief of staff (cos)** fires in four modes: cycle (after substantive integrate), reflective (clock slots), user_reply (email/iMessage/WhatsApp self-message in), command (notch chat/voice). Each mode spawns a fresh Claude subprocess with the Deja MCP attached.
- **Voice + command** classifier: hold Option and speak (or type in the notch panel); cos receives the utterance directly and routes it.

Both reactive layers write back into the same substrate the pipelines read: the wiki, `goals.md`, and `observations.jsonl`.

## 3. Process model

Deja is a two-tier system.

**Swift menubar app (`/Applications/Deja.app`)** — the UI shell. Entry at `menubar/Sources/App/AppDelegate.swift`. On launch:

- Sets `.accessory` activation policy (menubar only, no Dock icon).
- Instantiates `MonitorState` (the central state object, `menubar/Sources/Services/MonitorState.swift`, ~1200 lines).
- Spawns two Python subprocesses via `BackendProcessManager`:
  - `deja monitor` — the observe/integrate/reflect loop.
  - `deja web` — a FastAPI server on a Unix socket at `~/.deja/deja.sock` (filesystem permissions `0700` = auth).
- Starts the floating voice pill (`VoicePillWindow`), the hotkey listener (`HotkeyManager`), the keystroke monitor (`KeystrokeMonitor`), the screen capture scheduler (`ScreenCaptureScheduler`), and the typed-content monitor (`TypedContentMonitor`).
- If first-run: opens `SetupPanelView`.

**Python backend** — the brains. Two processes, both children of the Swift app:

- `deja monitor`: runs the three pipelines. See `src/deja/agent/loop.py`.
- `deja web`: FastAPI app at `src/deja/web/app.py`. Handles voice endpoints, setup, meeting recording, MCP bootstrap.

The Swift app talks to the backend over HTTP via the Unix socket. A few state handoffs use file markers under `~/.deja/` (e.g., `voice_cmd.json`, `notification.json`) — older patterns kept because they work.

Both Python processes enforce single-instance via PID files (`~/.deja/monitor.pid`, `~/.deja/web.pid`). Old instances get SIGTERM on start.

## 4. Data model

### 4.1 The wiki at `~/Deja/`

```
~/Deja/
├── index.md                 # auto-generated, time-sorted catalog
├── goals.md                 # tasks, waiting-fors, reminders, standing context
├── log.md                   # event log (manually inspected)
├── reflection.md            # daily synthesis
├── people/<slug>.md         # one page per person
├── projects/<slug>.md       # one page per ongoing project
├── events/YYYY-MM-DD/<slug>.md   # timestamped events with YAML frontmatter
├── observations/YYYY-MM-DD.md    # daily narrative log
├── conversations/YYYY-MM-DD/<slug>.md  # user↔cos dialogues (per thread)
├── prompts/                 # editable LLM prompts (integrate, onboard, etc.)
└── .git/                    # every write is a commit
```

**Entity pages** (people, projects) are prose — 100-400 words, present tense, lead with what's true now. Updates require a concrete new fact (Rule 8 in `src/deja/default_assets/prompts/integrate.md:56`). Standing facts get promoted to the entity body (Rule 9 — added to prevent one-off events from losing durable context).

**Event pages** have strict frontmatter:

```yaml
---
date: 2026-04-18
time: "11:01"
people: [david-wurtz, laura-parker-ellas-mom]
projects: [miles-gymnastics]
---
```

Event metadata is emitted by the integrate LLM as a structured `event_metadata` field; the wiki writer serializes it into YAML. You don't write frontmatter by hand for events.

**index.md is load-bearing.** `src/deja/wiki_catalog.py` rebuilds it after every wiki change — a flat list of every page, sorted by mtime descending (most-recently-touched first), with a one-line summary. Three consumers read it top-down within their attention budget:

- `wiki_retriever.build_analysis_context()` — integrate's LLM context.
- `deja.llm.prefilter.triage_batch()` — the triage prefilter.
- `deja.vision_local._build_prompt()` — the vision prompt (truncated via `max_lines`).

So the ordering isn't cosmetic — it directly decides what the LLMs see first.

### 4.2 `goals.md` — the working ledger

Editable in Obsidian, read + written by the agent. Five sections:

- `## Standing context` — durable rules ("David drives carpool Mon/Wed", "Pool chlorine weekly").
- `## Automations` — trigger→action rules ("TeamSnap email → Calendar event").
- `## Tasks` — `[ ]` / `[x]` lines.
- `## Waiting for` — items other people owe the user.
- `## Reminders` — date-keyed nudges (`[YYYY-MM-DD] question → [[topics]]`).
- `## Archive` — resolved items with timestamps + reasons.

Mutated via `src/deja/goals.py:apply_tasks_update()`. The integrate LLM emits a `tasks_update` block with add/complete/archive/resolve keys; the agent applies them atomically.

Cos's disposition is to **add to goals.md rather than email** for anything non-urgent. Goals.md becomes cos's scratchpad of "things I'm thinking about for the user" — future cos cycles read it and decide *when* (or whether) to surface each item. See the cos section.

### 4.3 `observations.jsonl`

Append-only JSONL at `~/.deja/observations.jsonl`. One row per observation. Schema roughly:

```json
{
  "timestamp": "2026-04-18T11:28:52.123456",  // naive local
  "source": "imessage" | "email" | "screenshot" | "whatsapp" | "calendar" | "browser" | "clipboard" | "typed" | "imessage_send" | ...,
  "sender": "Laura Parker (+19282467409)",
  "text": "Laura said she may drop Miles off at reach 11 FYI",
  "id_key": "chat401228334969608945-199873",  // dedupe handle
  "chat_id": "...",                             // for thread context
  "chat_label": "...",                          // for display
  // ... source-specific fields
}
```

**Timestamps are naive local**, not UTC. This is a real-world pain point — a prior bug stamped them with `tzinfo=UTC` and silently dropped every screenshot on non-UTC hosts for 6 days. See `src/deja/agent/analysis_cycle.py:40-85` for the stale-screenshot filter that has to compare naive-local with naive-local.

### 4.4 `audit.jsonl`

Every state mutation writes one row here. Inspected with `deja trail`. Schema:

```json
{
  "ts": "2026-04-18T18:14:38Z",
  "kind": "goal_action" | "cos_invoke" | "cycle" | "reflect" | "voice_transcript" | "voice_undo" | "reply_auth_reject" | ...,
  "target": "action/calendar_create" | "cycle/c_8fa43a1226b3" | "projects/<slug>" | ...,
  "reason": "concrete human-readable why",
  "trigger": {"kind": "mcp" | "signal" | "manual" | "webhook" | "cron", "detail": "..."}
}
```

`trigger.kind` tells you who drove the mutation:

- `signal` — an integrate cycle (observe → analysis → wiki write).
- `mcp` — a write came through the MCP server. The common `detail` is `hermes` (see below).
- `manual` — user or CLI (`deja cos test`, `deja linkify`, etc.).
- `webhook` / `cron` — external trigger.

**Hermes is the old name for cos**, kept as the MCP-surface audit tag. When you see `trigger.kind=mcp, trigger.detail=hermes` in `audit.jsonl`, that means cos did the action (via one of its MCP write tools). The tag is stamped in `mcp_server.py:1115` — every MCP-invocation sets `audit.set_context(trigger_kind="mcp", trigger_detail="hermes")` before dispatching tool handlers. The CLI alias `deja hermes-trail` is also retained for back-compat.

Rows older than 7 days are trimmed during each reflect pass.

### 4.5 `~/.deja/` — complete catalog

| File | Purpose |
|---|---|
| `observations.jsonl` | Signal log (above). |
| `audit.jsonl` | Audit trail (above). |
| `config.yaml` | User config — feature flags, slot hours, integrate model. |
| `deja.log` | Rotating log file. |
| `deja.sock` | Unix socket FastAPI listens on. |
| `monitor.pid` / `web.pid` | Single-instance PID files. |
| `setup_done` | Marker — first-run setup complete. |
| `health.json` | TCC permission state (polled by Swift app). |
| `latest_error.json` | Transient error surface (dismissible toast). |
| `notification.json` | Agent-issued user notification (Swift polls, then unlinks). |
| `voice_cmd.json` / `voice_status.json` | Swift↔Python IPC for voice recording. |
| `audio/session-<ts>.wav` | Captured WAV files from voice dictation. |
| `last_integration_offset` | Byte offset into `observations.jsonl` — resumption mark. |
| `last_reflection_run` | ISO timestamp — most recent successful reflect pass. |
| `integrate_trigger.json` | Cross-process trigger: write this to fire integrate immediately. |
| `raw_ocr/<YYYY-MM-DD>/<id_key>.txt` | Apple Vision OCR text from screenshots (consumed by OCR-only paths and debugging). |
| `raw_images/<YYYY-MM-DD>/<id_key>.png` | Raw screenshot PNGs (consumed by the integrate Claude Vision path). |
| `imessage_buffer.json` | Latest iMessage snapshot (dedupe). |
| `contacts_buffer.json` | Google Contacts snapshot. |
| `gmail_history_cursor.txt` | Gmail incremental-sync cursor. |
| `calendar_sync_tokens.json` | Calendar incremental-sync tokens. |
| `chief_of_staff/` | Cos config + state (detailed in §7). |
| `integrations.jsonl` | Integrate cycle outputs before wiki writes. |
| `conversation.json` | Voice history shown in the notch panel. |
| `typed_content.jsonl` | Snapshots of focused text fields (post typing-pause). |
| `latest_screen.png` + `latest_screen_ts.txt` | Most recent screenshot + when. |
| `screen_<N>.png` / `screen_<N>_ax.json` | Per-display screenshots + AX sidecars. |
| `notification.json` | Agent-initiated user notification. |

## 5. The observe pipeline

Signals flow through three stages before becoming wiki content. The first stage (observe) happens every 3 seconds; the second (integrate) every 5 minutes; the third (reflect/cos) on demand or on a clock.

```
  SOURCES                    observe (3s)             integrate (5 min)
┌──────────────┐          ┌─────────────────┐       ┌────────────────┐
│ iMessage     │          │  Observation    │       │ Flash-Lite /   │
│ WhatsApp     │─────────►│  collect()      │       │ Flash LLM      │
│ Email        │  raw     │  per source     │       │ reads new      │
│ Screenshot   │  rows    │                 │       │ signals +      │
│ Calendar     │          │  → Observation  │       │ retrieved wiki │
│ Drive        │          │    object       │       │ context        │
│ Browser      │          │                 │       │                │
│ Clipboard    │          │  dedupe by      │       │ emits:         │
│ Voice        │          │  id_key         │       │  - wiki writes │
└──────────────┘          │                 │       │  - events      │
                          │  tier (T1/T2/T3)│       │  - goal muts   │
                          │                 │       │  - narrative   │
                          │  persist →      │       │                │
                          │  ~/.deja/       │       │                │
                          │  observations   │──────►│  reads offset, │
                          │  .jsonl         │       │  advances it   │
                          └─────────────────┘       └────────┬───────┘
                                                             │
                                                             ▼
                                                 ┌────────────────────┐
                                                 │ substantive cycle? │
                                                 └────────┬───────────┘
                                                          │ yes
                                                          ▼
                                                   cos (cycle mode)
                                                   — MCP attached,
                                                   reads wiki + goals,
                                                   decides: NOTIFY /
                                                   ACT / SILENT
```

`src/deja/agent/loop.py` orchestrates three async loops:

1. **Signal loop** (`_signal_loop`, `observation_cycle.py:150-354`) — every 3 seconds.
2. **Analysis loop** (`_analysis_loop`, `analysis_cycle.py:297-343`) — every 300 seconds.
3. **Watchdog** (`_watchdog_loop`, `loop.py:278-295`) — every 60 seconds, checks collector liveness.

### 5.1 Sources and cadences

Observation sources live in `src/deja/observations/` and all subclass `BaseObserver` (`base.py`). Each implements `collect()` → yields `Observation` objects. Cadences within the 3s signal loop:

- iMessage, WhatsApp, clipboard — every cycle (3s).
- Email, calendar, drive, tasks, meet — every 2 cycles (~6s).
- Screenshot — every 2 cycles (~6s), event-driven via `ScreenCaptureScheduler`.
- Browser — every 3 cycles (~9s).

The Observer orchestrator (`collector.py:31-206`) dedupes by `id_key`, then appends to `observations.jsonl` via `_persist_signal()` (line 278). The last byte offset is saved to `~/.deja/last_integration_offset` so the next analysis cycle knows where to resume.

### 5.2 Screenshots — two-stage pipeline

Screenshots are the only source with serious post-processing:

1. **Capture** (Swift `ScreenCaptureScheduler`). Event-driven: app focus change, typing pause ≥2s, AX window-change, 60s passive floor. The old 6s fixed timer captured ~14k/day; the new scheme captures ~1k/day — fewer redundant frames, each marking a state transition.
2. **Local OCR**. Apple Vision (~1.5s on-device). Text is saved to `~/.deja/raw_ocr/<date>/<id_key>.txt` before any further processing — preserved for OCR-only consumers and debugging.
3. **Raw image sidecar**. The PNG is saved to `~/.deja/raw_images/<date>/<id_key>.png`. The integrate Claude Vision path reads these pixels directly — it does not consume the preprocess summary.
4. **Preprocess** (only if OCR ≥400 chars). `screenshot_preprocess.py` calls Gemini Flash-Lite to condense the OCR text: strip chrome, structure as TYPE/WHAT/SALIENT_FACTS, or return None to SKIP entirely. Skipped screenshots are dropped. The condensed text is consumed by the OCR-only paths (e.g. command-mode `recent_screens` snapshot, debugging); the integrate cycle bypasses it and feeds Claude the raw PNG.
5. **Persist** to `observations.jsonl` only after all of the above.

### 5.3 Thread context injection

For iMessage/WhatsApp, the formatter (`src/deja/signals/format.py:86-146`) reaches backward through `observations.jsonl` and reconstructs the last 30 messages in the same thread. These are prepended as `## Context — already processed` so the integrate LLM can understand a terse reply ("ok", "sounds good") without guessing the referent.

### 5.4 Tiering — T1/T2/T3

`src/deja/signals/tiering.py:classify_tier()` is a pure function that labels each signal as:

- **T1** — user-authored or inner-circle inbound. Always kept; promoted in integrate.
- **T2** — focused attention (active threads, current calendar context).
- **T3** — ambient / background (passive browser, noise).

User email(s) and inner-circle slugs are loaded once per process from the wiki. Pure, deterministic. The integrate pipeline drops T3-only message noise (automation emails, off-catalog mentions) and always keeps T1/T2.

## 6. The integrate pipeline

Fires every 300 seconds (default; `INTEGRATE_INTERVAL` in `config.py`), or immediately when `~/.deja/integrate_trigger.json` is written (voice commands that classify as `context` do this; the chat input does too).

### 6.1 What it does

`src/deja/agent/analysis_cycle.py:run_analysis_cycle()`:

1. Read fresh signals from `observations.jsonl` since the last offset.
2. Filter stale screenshots (>30 min old; the regression guard at lines 40-85).
3. Triage signals via `classify_tier` (T1/T2/T3).
4. Rebuild `index.md` (`wiki_catalog.rebuild_index()` — fast, ~100-500ms).
5. Retrieve wiki context via `wiki_retriever.build_analysis_context()` — hybrid BM25 (entity tokens) + QMD vector search, always including `index.md`.
6. Call the integrate LLM with: formatted signals + retrieved wiki + `goals.md` (capped at 6000 chars) + current time + contacts summary.
7. Parse + apply outputs.
8. Emit webhooks + fire cos if the cycle was substantive.

### 6.2 Which LLM?

Integrate is hardcoded to Claude Opus 4.7 via the local `claude` CLI subprocess (since 2026-04-17). `claude -p --input-format stream-json` receives raw screenshot PNGs as multimodal content blocks alongside the formatted signals — Claude reads the pixels directly, so layout, focus indicators, calendar grids, and bold/gray emphasis are all available without an OCR intermediate. There is no `INTEGRATE_MODE` flag and no Gemini fallback for integrate; the prior shadow A/B eval was concluded and removed.

See `src/deja/integrate_claude_vision.py`.

The prompt itself is `src/deja/default_assets/prompts/integrate.md` — 200+ lines. Load-bearing rules (numbered 1-9) include: only write what signals say, deletion requires explicit user retraction, person pages require structured grounding (email/phone/chat_label/existing ref), update-without-new-fact is banned, durable facts get promoted to entity pages.

### 6.3 What it outputs

A single JSON object:

```json
{
  "observation_narrative": "one-line lead + bulleted threads",
  "reasoning": "one paragraph",
  "wiki_updates": [{ "category": "people|projects|events", "slug": "...", "action": "create|update|delete", "body_markdown": "...", "event_metadata": {...}, "reason": "..." }],
  "goal_actions": [{ "type": "calendar_create", "params": {...}, "reason": "..." }],
  "tasks_update": { "add_tasks": [...], "complete_tasks": [...], ... }
}
```

Applied in order by the agent:

- `wiki_updates` → `wiki.py:apply_updates()` → writes + git commits.
- `goal_actions` → `goal_actions.execute_all()` → side effects (calendar events, email drafts).
- `tasks_update` → `goals.py:apply_tasks_update()` → edits `goals.md`.
- `observation_narrative` → appended to `~/Deja/observations/YYYY-MM-DD.md`.

### 6.4 The observation narrative

A short structured snapshot of the cycle: one lead line, then bulleted threads (one per topic, specific with times/names/amounts). Rendered as a card in the notch panel's "Now" tab. The Swift side (`CommandCenterView.ObservationCard`) parses `- ` bullets and renders them indented.

## 7. The reflect pipeline

Clock-driven, three slots/day (default `02:00`, `11:00`, `18:00` local — configurable via `REFLECT_SLOT_HOURS`). Triggered by `should_run_reflection()` (`src/deja/reflection_scheduler.py:100`). Slot boundaries are missed-fire resistant: if the machine was asleep and the clock crosses a slot, the next wake triggers the pass once (not a catch-up stampede).

Reflect used to be a sequence of narrow Flash-Lite confirmation sweeps — dedup-confirm, events→projects proposal, goals reconcile, contradiction classification — each with its own prompt, its own JSON contract, and its own class of false positives. That's gone. The new shape is a thin deterministic prep step that produces *candidates*, and one cos invocation that decides what to do about them.

```
  DETERMINISTIC PREP              COS REFLECTIVE                 WRITES
┌──────────────────────┐        ┌──────────────────┐        ┌────────────────┐
│ qmd update           │        │  claude -p       │        │ wiki merges /  │
│ qmd embed            │        │  (fresh subproc, │        │ updates        │
│ (refresh QMD         │──────► │   MCP attached)  │──────► │                │
│  vector index)       │        │                  │        │ goals.md:      │
│                      │        │ calls the four   │        │  new tasks,    │
│ (audit trim          │        │ find_* tools     │        │  notes,        │
│  runs AFTER cos)     │        │ on demand, plus  │        │  contradictions│
│                      │        │ search_deja,     │        │                │
│                      │        │ gmail_search,    │        │ optional       │
│                      │        │ get_page,        │        │  [Deja] email  │
│                      │        │ recent_activity, │        │  (send_email_  │
│                      │        │ calendar_*       │        │   to_self)     │
│                      │        │                  │        │                │
│                      │        │ decides per      │        │                │
│                      │        │ three-path       │        │                │
│                      │        │ escalation (§7.3)│        │                │
└──────────────────────┘        └──────────────────┘        └────────────────┘

  Note: the find_* tools (find_dedup_candidates, find_orphan_event_clusters,
  find_open_loops_with_evidence, find_contradictions) are MCP tools, not prep
  steps. Cos invokes them from inside its loop — if it doesn't ask, the work
  doesn't run.
```

### 7.1 Deterministic prep

`run_reflection()` (in `src/deja/reflection_scheduler.py:134`, re-exported from `deja.reflection` for back-compat) is deliberately thin:

1. **Refresh the QMD vector index** (`qmd update` then `qmd embed`) so cos's candidate-generator tools and `search_deja` both see the current wiki state. Failure is non-fatal — reflect continues against stale embeddings and logs.
2. **Invoke cos in reflective mode.** `chief_of_staff.invoke_reflective_sync()` — one subprocess, one decision loop.
3. **Audit trim** — `trim_older_than(days=7)` drops stale rows from `audit.jsonl`.

No LLM calls outside cos itself. The cheap analyst work that used to live here (dedup-confirm, events→projects proposal, goals reconcile, contradiction classification) is gone as an explicit step — the candidate-generation logic survives but now lives behind the `find_*` MCP tools, which cos calls only when it decides to. If cos chooses not to ask for a given candidate set this slot, that work never runs.

### 7.2 Cos reflective invocation

A single `chief_of_staff.invoke_reflective_sync()` call. Cos reads its system prompt + REFLECTIVE_APPENDIX, sees the slot (02/11/18) and the horizon, and decides what to look into. It has four new MCP candidate-generator tools on top of its usual read/write surface:

- `find_dedup_candidates(category, threshold, limit)` — people/project page pairs above a vector-similarity threshold. Cos reads both pages, checks `search_deja` for disambiguating context, decides whether they're the same entity, and if so calls `update_wiki` to merge.
- `find_orphan_event_clusters(min_size, sim_threshold)` — clusters of events that look like they should have a parent project page. Cos decides whether to materialize a `projects/` page (via `update_wiki` create) or leave them as scattered events.
- `find_open_loops_with_evidence(days, limit)` — open tasks and waiting-fors paired with recent events that might resolve them. Cos decides whether a loop is genuinely closed (call `complete_task` / `resolve_waiting_for`), still open, or needs the user's attention.
- `find_contradictions(sim_min, sim_max, limit)` — page pairs in the mid-similarity window (close enough to be about the same thing, far enough apart to possibly disagree on a fact). Cos reads both sides and decides what to do with each pair per §7.3. This replaces the disabled Flash contradictions sweep, which produced too many false positives stripping real facts.

Because cos is the one deciding, verification is no longer a rigid prompt contract — cos can call `gmail_search`, `search_deja`, `get_page`, `recent_activity`, `calendar_list_events` to gather evidence before it commits to a call. That's the whole point of the refactor: judgment moves from a dozen narrow Flash calls to one capable agent with tools.

### 7.3 Three-path escalation pattern

Any time cos faces a judgment call on a `find_*` candidate — dedup pairs, contradictions, maybe-closed open loops — it picks one of three dispositions:

- **Resolvable via tools** → cos silently fixes via the appropriate write: `update_wiki` / `complete_task` / `resolve_waiting_for`. If `gmail_search`, `calendar_list_events`, or `search_deja` can adjudicate, the user doesn't need to. The mutation's `reason` names the evidence that closed the call.
- **Unresolvable but not blocking** → cos writes a note to `goals.md` with both claims (or both candidates) and the tool evidence it checked. Future cos cycles see the note and can revisit if new signals land.
- **Blocking an open loop or critical fact** → cos asks the user via `send_email_to_self`. Just the question and the two claims / candidates, no padding. The user's reply routes back through the user_reply channel (§12) and cos resolves the write.

Fix silently if you can, write to goals if you can't but it's not urgent, email the user only when you genuinely need them. This is how `find_contradictions` was revived after the Flash classifier was disabled — not with a tighter prompt, but by making the escalation path explicit and giving cos the tools to execute it.

### 7.4 What's gone

- `goals_reconcile.py` — cos handles this via `find_open_loops_with_evidence`. The dedicated Flash sweep is removed.
- The Flash-confirm step inside `dedup.py` — cos reads the pages and decides. The candidate-pair generation logic remains as the backing store for `find_dedup_candidates`.
- The Flash proposal step inside `events_to_projects.py` — same pattern: clustering stays as the analyst layer behind `find_orphan_event_clusters`, cos makes the materialization decision.
- The disabled contradictions sweep — revived, but as a cos-driven tool rather than a standalone Flash classifier.

## 8. Chief of Staff (cos)

Cos is Deja's reflex layer. It's Claude (via the `claude` CLI subprocess), fired on four trigger types, reading state via the Deja MCP, deciding whether to notify you, take action, or stay silent.

### 8.1 Cos as decision layer

The mental model for the whole system:

```
  ANALYSTS (cheap, high-volume,              COS (one capable
     deterministic)                            agent with tools)
┌───────────────────────────────────┐      ┌───────────────────┐
│ tiering (T1/T2/T3)                │      │  Claude Opus      │
│ Flash preprocess (screenshots)    │      │  via claude -p    │
│ vector similarity (QMD)           │ ───► │                   │ ───►  writes
│ clustering                        │      │  decides          │      + actions
│ candidate generators (reflect)    │      │                   │
│ Apple Vision OCR                  │      │  MCP tool surface:│
│ BM25 retrieval                    │      │   read, write,    │
└───────────────────────────────────┘      │   verify, escalate│
                                           └───────────────────┘
```

Analysts are cheap and narrow. They're good at "here are 40 pairs of pages above 0.82 cosine similarity" or "this screenshot has 820 chars of OCR, condense or skip." They're terrible at "which of these pairs is actually the same person" — that's a judgment call involving disambiguation against tools, and it's exactly where Flash-confirm sweeps used to fail.

Cos is the decision layer. It doesn't need to be cheap because it runs rarely (three reflect slots/day, plus substantive integrate cycles, plus user-initiated commands). It does need to be capable — Claude Opus with a full MCP tool surface, able to call `gmail_search` / `search_deja` / `calendar_list_events` to verify before it writes, and able to ask the user via `send_email_to_self` when it genuinely can't resolve something.

Practical consequence: when you're tempted to add a narrow LLM sweep somewhere — "let's have Flash classify these events as X or Y" — the right move is almost always to (a) add a deterministic candidate generator and (b) expose it as an MCP tool so cos can decide. The integrate pipeline is the exception (it's immediate, bounded, and its prompt is load-bearing); reflect is explicitly designed around cos-makes-the-call.

### 8.2 Invocation modes

```
                       ┌────────────────────────────────┐
                       │        claude -p               │
                       │ (fresh subprocess per call,    │
                       │  --mcp-config → Deja MCP,      │
                       │  10-min hard timeout)          │
                       └──────────────▲─────────────────┘
                                      │ invoke_*_sync()
             ┌────────────┬───────────┼───────────┬──────────────┐
             │            │           │           │              │
         ┌───┴───┐  ┌─────┴────┐  ┌───┴──────┐  ┌─┴────────────┐
         │ cycle │  │reflective│  │user_reply│  │  command     │
         └───▲───┘  └────▲─────┘  └────▲─────┘  └──────▲───────┘
             │           │             │               │
    substantive     clock slots    user→user msg    notch chat /
    integrate       (02/11/18)     in ANY channel:  voice  push-
    cycle                          email, iMessage  to-talk
                                   self-chat,
                                   WhatsApp self-
                                   chat
             │           │             │               │
        DEFAULT    + REFLECTIVE   + USER_REPLY    + COMMAND
        SYSTEM      APPENDIX       APPENDIX        APPENDIX
        PROMPT                                     (+ recent_screens
                                                     preloaded)
```

| Mode | Trigger | Payload | System-prompt appendix |
|---|---|---|---|
| `cycle` | After a substantive integrate cycle | `{mode, cycle_id, narrative, wiki_update_slugs, goal_changes_count, due_reminders_count, new_t1_signal_count}` | DEFAULT_SYSTEM_PROMPT |
| `reflective` | Clock slot (02/11/18, runs inside `run_reflection`) | `{mode: "reflective", slot, horizon, ts}` | + REFLECTIVE_APPENDIX |
| `user_reply` | Any user→user message — email-to-self, iMessage self-chat, WhatsApp self-chat. No `Re: [Deja]` requirement anymore; ANY user→user message routes here. Only exception: cos's own outbound `[Deja] ...` emails (non-`Re:`) are dropped to avoid self-feeding. | `{mode: "user_reply", subject, user_message, thread_id, in_reply_to, conversation_slug}` | + USER_REPLY_APPENDIX |
| `command` | Notch chat / voice push-to-talk (`/api/command`, `/api/mic/stop`) | `{mode: "command", user_message, source, conversation_slug, recent_screens, ts}` | + COMMAND_APPENDIX |

Entry points in `src/deja/chief_of_staff.py`:

- `invoke()` / `invoke_sync()` — cycle mode.
- `invoke_reflective_sync()` — reflective mode.
- `invoke_user_reply_sync()` — user_reply mode (called by self-channel observers).
- `invoke_command_sync()` — command mode (called by `/api/command` and mic stop).

All spawn `claude -p` with `--mcp-config` pointing at `~/.deja/chief_of_staff/mcp_config.json`, a 10-minute hard timeout, and the system prompt appended inline per mode.

**Command-mode screenshot preload.** Unlike the other modes (where the user is typically on their phone), the notch panel is a signal that the user is AT their Mac. `_recent_screens_snapshot()` in `chief_of_staff.py:1614` assembles `{display_id: {app, window_title, ocr, age_sec}}` from the freshest per-display OCR sidecars in `~/.deja/raw_ocr/<today>/` plus the AX sidecars at `~/.deja/screen_<N>_ax.json`. A 5-minute freshness gate drops stale displays entirely rather than misrepresent them as current. Cos reads this to resolve referents like "this email," "that person," "the window on the left" without paying a `recent_activity` tool call. Empty dict = no display had a fresh capture; cos is instructed (in `COMMAND_APPENDIX`) not to assume screen context in that case.

### 8.3 Decision tree (disposition)

Every invocation picks one of:

1. **NOTIFY** — send a push email via `execute_action("send_email_to_self", ...)`. **Only** for:
   - Action needed within ~24h that the user isn't already handling.
   - A fact the user believes is wrong or just changed.
   - A live opportunity about to close (reply window, in-person moment).
2. **ACT** — any MCP write: `add_reminder`, `add_task`, `update_wiki`, `calendar_create`, `draft_email`, `complete_task`, etc.
3. **SILENT** — do nothing. A day with no email is healthy.

The disposition is **filter, don't plan**. Goals.md is cos's scratchpad for things it's thinking about; cos reviews it every cycle and decides *when* to surface — considering time of day, day of week, whether the user is mid-coordination, natural batching opportunities. No fixed digest schedule; timing is a reasoning task.

### 8.4 Cos reasons over time

Cos is stateless per-invocation (each call is a fresh subprocess) but **stateful across time via its writes**:

- **`goals.md`** — scratchpad. Cos writes items with forward-dated surface times; future cos cycles read and decide whether to act.
- **Wiki** — durable facts cos updates; future cos retrieves for context.
- **`~/Deja/conversations/YYYY-MM-DD/<slug>.md`** — one Markdown file per user↔cos conversation thread (or per voice/chat session). Indexed by the same QMD catalog as events; retrievable via MCP `search_deja` / `get_page`. Supersedes the legacy single-file `conversations.jsonl`.

So cos can plant a thought now and revisit it later. A future cos reads what a prior cos wrote. This is the mechanism for getting more useful over time.

### 8.5 Cos guardrails in the system prompt

Two rules that matter enough to call out here (rather than leaving them buried in `DEFAULT_SYSTEM_PROMPT`):

- **Stale auto-reminder FYI rule.** When cos's only evidence for an event is an automated reminder from a service that silent-deletes on cancellation (TeamSnap event reminders, Eventbrite, auto-calendar-invites), it must cite the uncertainty rather than assert the event is on. Required format: *"Practice might still be on per the April 18 TeamSnap reminder — no cancellation email since, but TeamSnap silent-deletes. FYI."* Services that don't email on DELETE (only on add/update) make absence-of-cancellation evidence of nothing. See `chief_of_staff.py:541-556`.
- **No double `calendar_create` for the same underlying event.** Cos picks ONE kind per event — `firm`, `reminder`, or `question` — and calls `calendar_create` exactly once. A firm meeting plus a reminder for the same moment puts two overlapping blocks on the user's calendar; that's the bug this rule prevents. Calendar + `add_reminder` (goals.md) together is fine — different surfaces — but two `calendar_create` calls is not. See `chief_of_staff.py:694-708`.

### 8.6 `~/.deja/chief_of_staff/` layout

| File | Purpose |
|---|---|
| `enabled` | Empty marker. Presence turns cos on. `deja cos enable/disable` touches / unlinks it. |
| `system_prompt.md` | The DEFAULT_SYSTEM_PROMPT copy. Editable by the user; kept in sync with source via the agent. |
| `mcp_config.json` | MCP server config for the claude subprocess (points at `python -m deja mcp`). |
| `invocations.jsonl` | Log of every cos invocation (payload, rc, stdout/stderr). Tailable with `deja cos tail`. |
| `processed_replies` | Line-per-Message-Id dedupe for the email self-channel. |
| `processed_self_imessages` | Line-per-id_key dedupe for the iMessage self-chat channel. |
| `processed_self_whatsapp_messages` | Line-per-id_key dedupe for the WhatsApp self-chat channel. |

## 9. MCP server

`src/deja/mcp_server.py` exposes Deja's state + actions to cos (and to Claude Desktop / Claude Code / Cursor / Windsurf). Stdio transport; auto-configured during setup via `mcp_install.py`.

### 9.1 Read tools

| Tool | Purpose |
|---|---|
| `daily_briefing()` | One-shot: date, user profile, tasks, waiting-fors, reminders, active projects, recent narratives. |
| `search_deja(query)` | BM25 across people/projects/events/conversations + goals.md slice. |
| `get_page(category, slug)` | Full page read. `category` accepts `people`, `projects`, `events`, `conversations`. |
| `get_context(topic)` | Hybrid retrieval bundle: profile + QMD hybrid search + goals slice + last 60 min of observations. |
| `list_goals()` | Raw `goals.md` grouped by section. |
| `search_events(query, days?, person?, project?)` | Event-only filtered search. |
| `recent_activity(minutes)` | Observations from the last N minutes (keyword filterable). |
| `calendar_list_events(time_min, time_max, ...)` | Direct Google Calendar API — authoritative ground truth. |
| `gmail_search(query)` / `gmail_get_message(id)` | Gmail native query syntax + full message body. |
| `find_dedup_candidates(category, threshold, limit)` | People/project page pairs above a vector-similarity threshold. Reflect candidate generator — cos decides the merge. |
| `find_orphan_event_clusters(min_size, sim_threshold)` | Clusters of events that share people/projects and look like they want a parent project page. Reflect candidate generator — cos decides whether to materialize. |
| `find_open_loops_with_evidence(days, limit)` | Open tasks + waiting-fors paired with recent events that might resolve them. Reflect candidate generator — cos decides whether the loop is closed. |
| `find_contradictions(sim_min, sim_max, limit)` | Page pairs in the mid-similarity window that might disagree on a fact. Reflect candidate generator — cos resolves per the §7.3 escalation pattern. |
| `browser_ask(prompt, timeout_sec=180)` | Shells out to `claude -p --chrome "<prompt>"` — drives Claude's Chrome extension non-interactively against the user's logged-in sites (Google Photos, Slack, Spotify podcasts, TeamSnap, etc.). ~60-120s per call; flat-fee on the user's Claude Pro/Max plan, not metered. Use only for services Deja has no direct API for. Implementation at `mcp_server.py:1544`. **Caveat:** can be blocked by Cloudflare bot-challenge pages (TeamSnap is a known offender) — in that case cos is expected to report the auth wall back to the user rather than retry or hallucinate. |

### 9.2 Write tools

| Tool | Effect |
|---|---|
| `update_wiki(action, category, slug, content, reason)` | Create / update / delete a page. Git-committed. |
| `add_task` / `complete_task` / `archive_task` | `goals.md` tasks. |
| `add_waiting_for` / `resolve_waiting_for` / `archive_waiting_for` | Waiting-fors (21-day auto-expire). |
| `add_reminder` / `resolve_reminder` / `archive_reminder` | Date-keyed reminders. |
| `execute_action(type, params, reason)` | Route to `goal_actions` executor (see §10). |
| `draft_imessage(handle, text)` | Background-stage a Messages compose field via `open -g imessage://<handle>?body=<text>`. No focus steal — user reviews on next switch to Messages and presses Return to send. **Default for any outbound iMessage to another human**; non-destructive, user-in-the-loop. Implementation at `mcp_server.py:1629`. |
| `send_imessage(handle, text)` | Immediate send via AppleScript (`tell application "Messages" ... send`). No review. **Reserved for unambiguous user directives** ("text X that Y") or self-chat acks. TCC caveat: first use requires a one-time Apple Events → MobileSMS approval from inside Deja.app. Implementation at `mcp_server.py:1665`. |

All writes tag the audit entry with `trigger.kind=mcp`, so `deja trail` shows both the trigger and the resulting mutation.

## 10. Actions — `goal_actions.py`

`src/deja/goal_actions.py` registers executors in `_EXECUTORS` keyed by action type. `execute_action(action)` dispatches by type; all exceptions are caught so one bad action doesn't block a batch.

### 10.1 The executors

| Type | Params | Side effect |
|---|---|---|
| `calendar_create` | `summary, start, end, location?, description?, kind?` | Google Calendar insert. `kind` is `firm` (default, no prefix, default reminders) / `reminder` (auto-prefix `[Deja] `, popup at event start) / `question` (auto-prefix `[Deja] ❓ `). Dedupes on un-prefixed title within ±1h window. |
| `calendar_update` | `event_id, ...` | Modifies an existing event. |
| `draft_email` | `to, subject, body` | Creates a Gmail draft. Never sends to third parties. |
| `send_email_to_self` | `subject, body, in_reply_to?, thread_id?` | Immediate send to the user's own address. Subject gets `[Deja] ` prefix if missing. Threading params enable in-thread replies on user_reply mode. |
| `create_task` / `complete_task` | `title, ...` / `task_id` | Google Tasks API. |
| `notify` | `title, message` | Writes `~/.deja/notification.json`; Swift app polls and shows. |

### 10.2 Undo support

Voice commands that create reversible artifacts get a short-TTL undo token (15s server-side, 5s UI). Implementation via a `ContextVar` artifact sink (`goal_actions.py:85-116`). `execute_with_artifacts()` captures what each executor created; `_dispatch_action` in `command_routes.py` registers the artifacts under a token; `POST /api/command/undo/{token}` reverses them.

Supported artifact kinds: `calendar_event` (delete via events().delete), `goal_line` (archive via `apply_tasks_update({"archive_tasks": [...]})`).

## 11. Voice + command pipeline

### 11.1 Capture

`HotkeyManager.swift` polls `NSEvent.modifierFlags` at 60Hz for Option (⌥). On down: plays a "Tink" system sound, calls `MonitorState.startVoiceCapture()` → POST `/api/mic/start`. On up: plays a release tone, POST `/api/mic/stop`.

`VoiceRecorder.swift` runs inside the Deja.app process (in-process, single TCC mic entry at `com.deja.app`). `VoiceCommandDispatcher.swift` polls `~/.deja/voice_cmd.json` and `voice_status.json` at 150ms — Python writes commands, Swift writes status.

### 11.2 Transcribe + polish

`/api/mic/stop` in `src/deja/web/mic_routes.py`:

1. Wait for Swift `done` status (up to 5s).
2. Groq Whisper via the proxy transcribes the WAV.
3. Groq `llama-3.1-8b-instant` polishes: grammar, fillers, spoken symbols → chars. Doesn't change word choice.
4. Hard-coded Whisper hallucination filter (drops "you", "thanks", "bye" as the entire transcript — Whisper does this on near-silent audio).

### 11.3 Classify

A single Flash-Lite call classifies into one of five types (see `src/deja/web/command_routes.py:_classify`):

| Type | Dispatcher | Example |
|---|---|---|
| `action` | `_dispatch_action` → goal_actions executor | "put dentist on my calendar tomorrow 3pm" |
| `goal` | `_dispatch_goal` → `goals.py:apply_tasks_update` | "remind me to reply to Matt" |
| `automation` | `_dispatch_automation` → appends to `goals.md ## Automations` | "when Amanda emails me about the theme, auto-draft a reply" |
| `context` | `_dispatch_context` → appends to `observations.jsonl` + fires integrate | "note that Ruby said her foot still hurts" |
| `query` | `_dispatch_query` → synthesizes answer from wiki+goals+activity | "what did Jon say about the casita quote?" |

### 11.4 UI feedback

On response, `MonitorState` renders the echo pill for 3 seconds:

- `[📅] transcript` — badge by classification type (📅 action, ✓ goal, 🔁 automation, 🧠 context).
- Confirmation line below in secondary style ("Created event: Dentist, Fri 3:00pm").
- **Undo button** for 5 seconds when the dispatch was reversible. Click → POST `/api/command/undo/{token}` → reverses the artifact → shows "Undone" in orange for 2s.

## 12. User→cos self-channel (email, iMessage, WhatsApp)

Any user→user message — email-to-self, iMessage self-chat, WhatsApp self-chat — routes straight to cos in `user_reply` mode. This gives the user one uniform "tell cos anything, from anywhere" channel that works on phone, desktop, or any client, without needing vision to see a screen.

The routing is channel-specific but the contract is the same: suppress the normal observation flow, log the user turn to the per-thread conversation file, and fire `chief_of_staff.invoke_user_reply()` non-blocking.

### 12.1 Email (`observations/email.py`)

`_build_observation_from_thread()` catches self-emails where `From` = `To` = user's Gmail identity. Subject no longer needs to start with `Re: [Deja]` — *any* user-to-user email routes to cos. The only exception is cos's own outbound `[Deja] ...` notifications (non-`Re:`), which are dropped at `email.py:739-745` to prevent a self-feeding loop.

The detailed handler is `_handle_user_reply_to_cos` (`email.py:559`). It:

1. Pulls `Message-Id` + `threadId` from the latest message in the thread.
2. Dedupes on `Message-Id` via `~/.deja/chief_of_staff/processed_replies` (Gmail history may re-scan the same message).
3. Runs `_verify_reply_auth()` anti-spoofing:
   - Parsed `From:` email must **exactly match** `load_user().email`. Substring matches rejected.
   - Gmail's `Authentication-Results` header must show `dmarc=pass` with `header.from=<user's domain>`. DMARC enforcement requires DKIM or SPF alignment.
4. Rejections audit as `reply_auth_reject` — visible in `deja trail`.
5. Logs the user turn to `~/Deja/conversations/<date>/<thread-slug>.md`.
6. Fires `chief_of_staff.invoke_user_reply()`.
7. Returns `None` so the thread never lands in `observations.jsonl`.

Cos reads the conversation file via MCP `get_page("conversations", "<date>/<slug>")` to see the full thread, uses `search_deja` for cross-thread topical lookups, and replies through `execute_action("send_email_to_self", {..., in_reply_to, thread_id})` which threads cleanly in Gmail.

### 12.2 iMessage self-chat (`observations/imessage.py`)

`_is_self_chat_turn()` (`imessage.py:69`) detects iMessage's built-in single-participant self-chat: `raw_speaker == "me"` and `chat_label` matches the user's own email, phone, or display name. Matching turns are routed to `_dispatch_self_imessage_to_cos` (`imessage.py:118`) which:

- Dedupes on `id_key` via `~/.deja/chief_of_staff/processed_self_imessages`.
- Uses `thread_id = "imessage-self-YYYYMMDD"` so a day's self-notes cluster in one conversation file.
- Logs the turn and fires `invoke_user_reply`.
- Skips the `results.append(...)` in `_collect_imessages` — integrate never sees it.

### 12.3 WhatsApp self-chat (`observations/whatsapp.py`)

Same contract as iMessage. `_dispatch_self_whatsapp_to_cos` (`whatsapp.py:114`) with `thread_id = "whatsapp-self-YYYYMMDD"` and dedupe via `~/.deja/chief_of_staff/processed_self_whatsapp_messages`. WhatsApp doesn't have a first-class "self-chat" UI the way iMessage does, but the `me → me` pattern works the same way once WhatsApp is configured to allow it.

## 13. Setup flow

### 13.1 First-run, from DMG to first useful cycle

1. User drags `Deja.app` to `/Applications`, opens it.
2. Swift app detects no `~/.deja/setup_done` marker → opens `SetupPanelView`.
3. **Google OAuth.** `connectGoogle()` → `/api/setup/gws-auth` → opens browser for consent on Gmail, Calendar, Drive, Tasks scopes → callback writes token to Keychain (or `~/.deja/google_token.json` as fallback).
4. **TCC grants.** Wizard walks through Screen Recording, Accessibility, Full Disk Access, Microphone. Each opens System Settings → Privacy & Security; the Swift app polls TCC APIs and updates the UI when each is granted.
5. **Vision model download.** `/api/setup/download-model` fetches the on-device FastVLM 0.5B weights (only if vision is enabled in that build).
6. **Backfill.** `/api/setup/start-backfill` spawns `python -m deja onboard --days 30` in a subprocess — ingests 30 days of sent email, iMessage, WhatsApp, calendar, Meet; bootstraps people/projects in the wiki.
7. **Complete.** User clicks "Start Déjà" → `/api/setup/complete` → writes `setup_done` marker → calls `install_mcp_servers()` to auto-register Deja with Claude Desktop / Code / Cursor / Windsurf.
8. **First monitor cycle.** Backend starts the observe pipeline. First integrate pass fires ~5 minutes later.

### 13.2 Code signing and TCC

Critical detail: Deja is signed with a stable `"Deja Dev"` identity (see `menubar/build.sh:26`). TCC grants are keyed to the signing identity's code digest. Ad-hoc signing (`--sign -`) creates a new identity per build, invalidating grants — you'd re-prompt for Screen Recording on every rebuild.

Bundled Python is also signed as an immutable sealed resource. That's why `PYTHONDONTWRITEBYTECODE=1` is set (`BackendProcessManager.swift:42`) and `bundle-python.sh` pre-compiles all `.pyc` files read-only — a runtime `.pyc` write breaks the seal, and Gatekeeper rejects the app with error -600.

### 13.3 MCP auto-install

`src/deja/mcp_install.py` detects installed AI clients and writes a config entry to each:

- Claude Desktop: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Claude Code: `~/.claude/mcp.json`
- Cursor: `~/.cursor/mcp.json`
- Windsurf: `~/.codeium/windsurf/mcp_config.json`
- VS Code (if detected)
- ChatGPT: skipped (only supports HTTP/SSE, not stdio)

The entry points at the absolute path of the dev venv's Python (not a symlink — stable across brew upgrades) running `-m deja mcp`.

## 14. Operating Deja

### 14.1 CLI surface

```
deja monitor              # the observe/integrate/reflect loop (spawned by the app)
deja web                  # the FastAPI backend (spawned by the app)
deja mcp                  # MCP server (stdio; launched by AI clients)
deja status               # liveness summary
deja configure            # CLI first-run setup
deja health               # startup checks + config diagnostic
deja onboard --days 30    # historical backfill
deja linkify [--dry-run]  # sweep wiki, wrap [[slug]] mentions
deja briefing             # print daily briefing (same view MCP serves)
deja cos status|enable|disable|test|reflect|tail|migrate-conversations
deja webhooks list|add|remove|test
deja trail                # recent audit entries
```

### 14.2 Debugging a cycle

- `deja trail` — what just happened.
- `~/.deja/deja.log` — full log.
- `~/.deja/observations.jsonl` — raw signals (tail -f).
- `~/.deja/chief_of_staff/invocations.jsonl` — what cos decided.
- `~/Deja/.git log` — every wiki mutation as a commit.

### 14.3 Disabling components

- `deja cos disable` — stops cos from firing.
- Delete `~/.deja/chief_of_staff/enabled` — same as `cos disable`.

## 15. Development workflow

### 15.1 From source

```bash
git clone https://github.com/dwurtz/deja.git
cd deja
python -m venv venv && ./venv/bin/pip install -e .
./venv/bin/python -m deja configure   # walks OAuth + permissions
./venv/bin/python -m deja monitor     # run the backend
```

For the Swift app, open `Deja.xcodeproj` in Xcode, or `make dev` at the repo root.

### 15.2 Make targets

- `make dev` — `xcodebuild -scheme Deja -configuration Release`, rsync to `/Applications/Deja.app`, kill + relaunch.
- `make dmg` — build + package `Deja.dmg` via `hdiutil`.
- `make bump VERSION=X.Y.Z` — update version strings in `pyproject.toml`, `project.yml`, `Deja-Info.plist`, `web/app.py`, `mcp_server.py`, `server/app.py`.
- `make release VERSION=X.Y.Z` — tag, push, GitHub Actions builds DMG.
- `make test` / `make test-swift` — pytest + xcodebuild test suites.

Sparkle auto-updates to the notarized builds published as GitHub Releases.

### 15.3 Prompts are editable

LLM prompts live in `src/deja/default_assets/prompts/`. First run copies them to `~/Deja/prompts/` — edits there override defaults and survive upgrades. Relevant prompts:

- `integrate.md` — the integrate-cycle contract.
- `onboard.md` — first-run backfill bootstrap.
- `command.md` — voice/chat classifier.
- `query.md` — query-type answers.
- Cos's system prompt lives separately at `~/.deja/chief_of_staff/system_prompt.md` (code fallback: `DEFAULT_SYSTEM_PROMPT` in `chief_of_staff.py`).

## 16. Extension points

Want to add a new observation source? Subclass `BaseObserver` in `src/deja/observations/` and register in `collector.py`. Your `collect()` yields `Observation` objects.

Want to add a new action? Add an executor function in `src/deja/goal_actions.py` and register in `_EXECUTORS`. If the action creates a reversible artifact, call `_record_artifact(...)` in the executor so voice undo works.

Want to add a new MCP tool? Add a handler in `src/deja/mcp_server.py`, register in the tool list. If it's a write, ensure the whitelist in `integrate_claude_vision.py` and `chief_of_staff.py` are consistent (reads and writes are gated separately).

Want to change the LLM behavior? Most of it is in the prompts. `integrate.md`, `command.md`, and the DEFAULT_SYSTEM_PROMPT in `chief_of_staff.py` are the three that matter for 90% of behavior. Prompts live in the repo but user overrides live in `~/Deja/prompts/` and `~/.deja/chief_of_staff/system_prompt.md`.

## 17. Gotchas and surprises

The ones you'll trip on if you don't know:

- **Naive-local timestamps everywhere.** `observations.jsonl` timestamps are naive local, not UTC. Comparing them to `datetime.now(timezone.utc)` silently drops everything on non-UTC hosts. Always compare naive with naive, or use `deja.observations.time_utils.parse_observation_ts`.
- **Index.md ordering is load-bearing.** Mtime-descending. If you touch a page, it jumps to the top and LLMs see it first. Don't "normalize" this.
- **TCC grants are per code-signing-identity.** Don't rebuild with ad-hoc signing in an environment where TCC has been granted — you'll re-prompt.
- **Bundled Python can't write .pyc at runtime.** Set `PYTHONDONTWRITEBYTECODE=1` or face Gatekeeper errors.
- **Screenshots are dedupe'd by perceptual hash.** Terminal frames with slightly different text can collide. `observations/screenshot.py` has a 90s time-based override for that case.
- **User→user messages (any channel) route straight to cos, not integrate.** Email-to-self, iMessage self-chat, WhatsApp self-chat — all bypass the observation log and fire cos in `user_reply` mode. The sole exception is cos's own outbound `[Deja] ...` emails (non-`Re:`), which are dropped to prevent a self-feeding loop. Practical consequence: reminders the user manually labels `[Deja]` in a subject line never enter the pipeline as observations — route them through the self-channel instead.
- **The integrate prompt Rule 7 is strict on person-page creation.** Names in screenshot OCR alone (no email, no phone, no prior `[[slug]]`) won't create people pages. This is intentional — prevents "ghost people" from inbox previews.
- **Preprocess gate is 400 chars.** Screenshots with <400 chars of OCR skip Flash-Lite and go raw to integrate. Tune `_PREPROCESS_MIN_CHARS` if you want more/less summarization.
- **Reflect is missed-fire-safe.** If the machine was asleep during a slot, the next wake runs the pass once. If multiple slots were missed, they coalesce — no stampede.
- **Voice undo token is in-memory.** Restart Deja.app within 15s of a voice dispatch and the token is gone. By design; short-lived.
- **Conversations were a single JSONL before; now they're per-file under `~/Deja/conversations/`.** Legacy file is renamed `conversations.jsonl.migrated` on first-run migration. Per-file layout makes them QMD-searchable like events.

---

Reference: the vision + backlog memory document at `/Users/wurtz/.claude/projects/-Users-wurtz-projects-deja/memory/project_deja_vision_and_backlog.md` captures what Deja is aiming to become, the "prove single-user first" strategy, and the organized backlog.
