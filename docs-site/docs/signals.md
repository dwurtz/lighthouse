# Signal sources

Deja watches about ten signal streams. Each one has its own quirks — how it's fetched, how it's deduped, how often, and what can go wrong. This page is a tour.

```mermaid
flowchart LR
    subgraph Native["Native macOS"]
        IM[iMessage]
        WA[WhatsApp]
        CL[Clipboard]
        SS[Screenshots]
        TY[Typed content]
        VO[Voice]
    end

    subgraph Google["Google Workspace"]
        EM[Gmail]
        CA[Calendar]
        DR[Drive]
        TK[Tasks]
        MT[Meet transcripts]
    end

    subgraph Browser
        BR["Browser history"]
    end

    Native --> Obs["observations.jsonl<br/>(append-only)"]
    Google --> Obs
    Browser --> Obs

    classDef nm fill:#1a365d,stroke:#2c5282,color:#f7fafc
    classDef gw fill:#744210,stroke:#975a16,color:#fefcbf
    classDef obs fill:#22543d,stroke:#2f855a,color:#f7fafc
    class Native,IM,WA,CL,SS,TY,VO nm
    class Google,EM,CA,DR,TK,MT gw
    class Browser,BR nm
    class Obs obs
```

All sources share one output: append a row to `observations.jsonl`. All sources share the same tiering system (T1/T2/T3, see [pipelines](pipelines.md#tiering)). What differs is how each one finds new material.

## Messages — iMessage and WhatsApp

Both read from local SQLite databases: `~/Library/Messages/chat.db` for iMessage, the WhatsApp desktop app's store for WhatsApp.

- **Cadence**: every 3-second cycle.
- **Dedupe**: per-message `id_key` (e.g. `chat<chat_id>-<message_rowid>`).
- **Contact resolution**: phone numbers and Apple IDs are matched against the macOS Contacts database to resolve display names.
- **Thread context**: when a new message lands, the formatter walks backward through `observations.jsonl` and attaches the last 30 messages in the same thread. That way the integrate LLM can understand a terse "ok" or "sounds good" without guessing.
- **Self-addressed messages** are a first-class channel. If you message yourself with `[Deja]` in the text, it routes to cos as a user-reply (same path as email replies).

Permissions required: Full Disk Access for the Deja app and its bundled Python binary (macOS doesn't let you read the Messages database without it).

## Email

Gmail via the Google Workspace API, incremental sync by `historyId`.

- **Cadence**: every ~6 seconds (every 2nd observe cycle).
- **Outbound** (sent by you) are T1 — always kept.
- **Inbound** go through the tiering function: close contacts → T2, automation / bulk → T3.
- **Self-emails with `[Deja]` subject** are inspected closely. A subject starting with `Re: [Deja]` routes to cos as a user reply. A subject without `Re:` is dropped — cos's own outbound, not a user message.
- **Anti-spoofing on reply detection** is strict: exact `From` match to the authenticated Google identity, plus a DMARC pass in `Authentication-Results`.

## Calendar

Google Calendar via incremental sync tokens.

- **Cadence**: every ~6 seconds.
- Captures events created, modified, or deleted since the last sync.
- Calendar is also readable live via MCP `calendar_list_events` — cos uses the API directly when it wants authoritative ground truth rather than the local observation log.

## Drive and Tasks

Same pattern: polled every ~6 seconds, delta-synced, tiering applied. Drive file opens and edits feed project context. Tasks (Google Tasks) is one of the write targets for `create_task` / `complete_task` MCP tools.

## Browser

A local history reader (SQLite again — Chrome/Arc/Safari depending on what's installed). Polled every ~9 seconds.

- Mostly T3 (ambient). A few domain patterns get promoted — a jobs board on an active job search, a project's GitHub org, etc.
- The current interface is passive. An `browser_ask` MCP tool lets cos query recent browsing when it's trying to ground a question ("what were you just reading about?").

## Clipboard

Polled every cycle for text copy events. Useful for catching "I just pasted this phone number from my calendar into my messages app" moments.

## Typed content

A typing-pause detector on focused text fields. When you stop typing for about 2 seconds in an input, Deja snapshots the current content. Particularly useful for catching long email drafts or chat messages before you send them.

## Screenshots {: #screenshots }

The only source with serious post-processing. Today, integrate reads the raw pixels directly.

```mermaid
flowchart LR
    subgraph Capture["On-device capture (local, free)"]
        Cap[ScreenCaptureScheduler<br/>event-driven]
        PNG[Raw PNG sidecar<br/>~/.deja/raw_images/]
        OCR[Apple Vision OCR<br/>sidecar ~/.deja/raw_ocr/]
    end

    subgraph Integrate["Integrate (5-min cycle)"]
        Claude[Claude Opus Vision<br/>reads PNG pixels directly]
    end

    Cap --> PNG
    Cap --> OCR
    PNG --> Claude
    OCR -.-> Debug[debugging / shadow eval<br/>not the primary path]

    classDef local fill:#1a365d,stroke:#2c5282,color:#f7fafc
    classDef primary fill:#22543d,stroke:#2f855a,color:#f7fafc
    classDef aside fill:#3d3d3d,stroke:#555,color:#ccc
    class Capture,Cap,PNG,OCR local
    class Integrate,Claude primary
    class Debug aside
```

### Capture is event-driven

The old captor ran on a fixed 6-second timer and caught about **14,000 frames a day**. Most were redundant. The new scheduler is event-driven: app focus change, typing pause (≥2s), accessibility window-change notification, or a 60-second passive floor. Result: about **1,000 frames a day**, each marking an actual state transition.

### Pixels go to Claude, not text

`INTEGRATE_MODE=claude_vision` is the production integrate path. For each screenshot in a cycle's batch, the raw PNG is base64-embedded as an `image` content block in a `claude -p --input-format stream-json` call. Claude Opus reads the pixels directly — focused-vs-inbox-preview distinction, calendar grid cells, bold/gray emphasis, layout — none of which survive an OCR intermediate. The PNGs are anchored with timestamp + display-label captions so Claude can reason about sequence ("frame 3 was 2s after frame 2" vs "4 minutes later").

### OCR still runs — but as a sidecar

Apple's Vision framework still OCRs every screenshot on-device in ~1.5s and saves the text to `~/.deja/raw_ocr/<date>/<id>.txt`. It's kept as a debugging aid — when an integrate decision looks wrong, the OCR sidecar tells you what text was actually on screen. Nothing downstream of the observation record reads the OCR sidecar in the production path — the raw PNG is what integrate sees.

## Voice

Push-to-talk via holding the Option (⌥) key. Capture happens in the Swift process (one TCC mic entry, `com.deja.app`). On release:

1. Groq Whisper transcribes via the LLM proxy.
2. Groq `llama-3.1-8b-instant` polishes — strips fillers, fixes spoken symbols ("comma" → `,`), preserves word choice.
3. Hard-coded filter drops known Whisper hallucinations on near-silent audio ("you", "thanks", "bye").
4. The polished transcript goes straight to cos in command mode (see below).

You can also type into the notch panel — same pipeline, same destination.

## Voice and chat — how commands are routed

Both go straight to cos in `command` mode. No intermediate classifier, no rule table — cos reads the utterance and decides what to do, with access to its full MCP tool surface plus a preloaded snapshot of whatever is currently on each display (`recent_screens` — freshness-gated at 5 min, per-display OCR + frontmost window metadata, so cos can resolve "that thing" or "this email" without a tool call).

```mermaid
flowchart LR
    Voice["Voice transcript<br/>(Groq Whisper + polish)"] --> Cos[cos · command mode]
    Chat["Notch chat text"] --> Cos
    Screens["recent_screens<br/>per-display OCR + AX"] -.preloaded.-> Cos

    Cos -->|unambiguous action| A["execute_action<br/>(calendar_create,<br/>draft_email, etc.)"]
    Cos -->|fact/preference| W["update_wiki"]
    Cos -->|open loop| R["add_reminder / add_task /<br/>add_waiting_for"]
    Cos -->|question| Q["search_deja + answer<br/>in the pill"]
    Cos -->|ambiguous| Clarify[ask for clarification]

    classDef in fill:#1a365d,stroke:#2c5282,color:#f7fafc
    classDef cos fill:#744210,stroke:#975a16,color:#fefcbf
    classDef out fill:#22543d,stroke:#2f855a,color:#f7fafc
    class Voice,Chat,Screens in
    class Cos cos
    class A,W,R,Q,Clarify out
```

Cos's final message is what shows in the notch pill — 1-3 lines, phone-readable, no preamble. Example replies: `Added: Dentist, Fri 3-3:30pm.` / `Noted on miles-gymnastics.md — safe-sport rule added.` / `Jane (Apr 16): quote coming next week; status looks fine.`

## Why all of this is worth the code

Having many sources lets Deja stay quiet in a different way: it can **cross-reference**. A calendar event on Friday + an iMessage on Thursday + a Gmail thread on Wednesday might be three pieces of the same project. A narrow classifier won't notice; cos, with the wiki as substrate and MCP as its hands, often will.

The whole thing only works because of the shared observations log and the wiki. Every source is just another small feeder into the substrate described in [the wiki](wiki.md).
