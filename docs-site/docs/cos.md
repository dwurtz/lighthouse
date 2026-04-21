# Chief of Staff

Cos is Deja's decision layer. Everything else — the tiering, the screenshot preprocess, the vector similarity, the candidate generators — is cheap analyst work preparing material for cos.

```mermaid
flowchart TB
    subgraph Analysts["Analysts — cheap, deterministic, high-volume"]
        T["Tiering (T1/T2/T3)"]
        P["Flash preprocess<br/>(screenshots)"]
        V["Vector similarity (QMD)"]
        CL[Clustering]
        CG["Candidate generators<br/>(find_dedup, find_contradictions, ...)"]
        OC["Apple Vision OCR"]
        BM[BM25 retrieval]
    end

    subgraph Cos["Cos — one capable agent with tools"]
        direction TB
        CA["Claude Opus<br/>via claude -p"]
        MCP["MCP tool surface:<br/>read · verify · write · escalate"]
        CA --- MCP
    end

    subgraph Outputs
        W[writes to wiki]
        G[writes to goals.md]
        E["[Deja] emails<br/>(only when needed)"]
        A[actions via MCP]
    end

    Analysts --> Cos
    Cos --> Outputs

    classDef an fill:#1a365d,stroke:#2c5282,color:#f7fafc
    classDef cos fill:#744210,stroke:#975a16,color:#fefcbf
    classDef out fill:#22543d,stroke:#2f855a,color:#f7fafc
    class Analysts,T,P,V,CL,CG,OC,BM an
    class Cos,CA,MCP cos
    class Outputs,W,G,E,A out
```

## Why this shape

Analysts are good at mechanical, narrow work: "here are 40 pairs of pages above 0.82 cosine similarity" or "this screenshot has 820 characters of OCR, condense or skip." They're terrible at "which of these pairs is actually the same person" — that's a judgment call, and it requires disambiguation against real-world evidence.

Cos doesn't need to be cheap because it runs rarely. It runs:

- After a **substantive integrate cycle** — maybe a dozen times a day.
- At **three reflect slots** (02 / 11 / 18 local) — three times a day.
- On **user-initiated commands** — voice, notch chat, self-addressed email or iMessage.

Maybe thirty to fifty Claude Opus calls per day total. That's cheap at current prices, and each call is worth it because cos is the one filtering what reaches you.

## Four invocation modes

Every cos invocation is a fresh `claude -p` subprocess with the Deja MCP attached and a 10-minute hard timeout. What changes is the trigger and the appendix to cos's system prompt.

```mermaid
flowchart TB
    subgraph Triggers
        T1["Substantive<br/>integrate cycle"]
        T2["Clock slot<br/>(02 / 11 / 18)"]
        T3["User-addressed<br/>email · iMessage · WhatsApp"]
        T4["Notch chat / voice<br/>push-to-talk"]
    end

    subgraph Modes["invoke_*_sync() entry points"]
        M1["cycle<br/>DEFAULT prompt"]
        M2["reflective<br/>+ REFLECTIVE appendix"]
        M3["user_reply<br/>+ USER_REPLY appendix"]
        M4["command<br/>+ COMMAND appendix<br/>(recent_screens preloaded)"]
    end

    subgraph Sub["claude -p subprocess<br/>(Deja MCP attached)"]
        C[Claude Opus]
    end

    T1 --> M1
    T2 --> M2
    T3 --> M3
    T4 --> M4
    M1 --> C
    M2 --> C
    M3 --> C
    M4 --> C

    classDef trig fill:#1a365d,stroke:#2c5282,color:#f7fafc
    classDef mode fill:#744210,stroke:#975a16,color:#fefcbf
    classDef sub fill:#22543d,stroke:#2f855a,color:#f7fafc
    class Triggers,T1,T2,T3,T4 trig
    class Modes,M1,M2,M3,M4 mode
    class Sub,C sub
```

| Mode | Trigger | Payload |
| ---- | ------- | ------- |
| `cycle` | After a substantive integrate cycle | cycle_id, narrative, wiki update slugs, goal changes count, due reminders count, new T1 signals |
| `reflective` | Clock slot (runs inside `run_reflection`) | slot, horizon, timestamp |
| `user_reply` | Self-addressed email / iMessage / WhatsApp | subject, user message, thread_id, in_reply_to, conversation slug |
| `command` | Notch chat or voice push-to-talk | user message, source, conversation slug, recent_screens, timestamp |

Command mode also preloads `recent_screens` — per-display OCR plus AX frontmost-window metadata — so cos can ground pronouns like "this email" or "that person" when you're verifiably at your computer.

## Disposition: NOTIFY · ACT · SILENT

Every invocation picks one of three dispositions.

```mermaid
flowchart LR
    Start["Cos invocation"] --> Decide{What does this<br/>actually need?}

    Decide -->|"Action within 24h<br/>or fact wrong<br/>or window closing"| Notify["NOTIFY<br/>send_email_to_self<br/>(or push notification)"]
    Decide -->|"Tool writes:<br/>task · reminder · wiki<br/>· calendar · draft"| Act["ACT<br/>execute via MCP"]
    Decide -->|"Thought is not urgent<br/>or evidence insufficient"| Silent["SILENT<br/>write to goals.md<br/>for future cos"]

    classDef dec fill:#744210,stroke:#975a16,color:#fefcbf
    classDef act fill:#22543d,stroke:#2f855a,color:#f7fafc
    class Start,Decide dec
    class Notify,Act,Silent act
```

- **NOTIFY** is reserved for three cases: an action needed within ~24 hours that the user isn't already handling; a fact the user believes is wrong or just changed; a live opportunity that's about to close.
- **ACT** is any tool write — add a task, add a reminder, update the wiki, draft an email, create a calendar event, complete a task. Actions leave a trail in `audit.jsonl`.
- **SILENT** is the default. A day with no email is healthy. If cos has a thought that isn't urgent, it writes it to `goals.md` and future cos cycles decide when to surface it.

The disposition is **filter, don't plan**. Goals.md is cos's scratchpad. There's no fixed digest schedule; timing is itself a reasoning task. Monday morning is often the right time to surface something noticed Friday afternoon.

## Cos reasons over time

Each cos invocation is a fresh subprocess — no in-process memory. But cos is **stateful across time via its writes**:

- **`goals.md`** is the scratchpad. Cos writes items; future cos cycles read them and decide whether to act.
- **The wiki** is durable facts cos updated; future cos retrieves them for context.
- **`~/Deja/conversations/<date>/<slug>.md`** is one file per user↔cos conversation thread. Indexed by the same QMD catalog as events; retrievable via MCP.

So cos can plant a thought now and revisit it later. A future cos reads what a prior cos wrote. This is the mechanism for getting more useful over time.

!!! example "Plant-and-revisit in practice"
    Friday 4 PM: an email arrives about a conference talk proposal due in two weeks. Cos notices, but it's Friday afternoon, the user is mid-coordination on unrelated work, and the deadline isn't imminent. Cos adds a reminder line to `goals.md` surfaced for Monday morning. Monday 11 AM reflect slot: cos sees the reminder, checks recent activity to see if the user has already started, and either prompts or lets it slide another day.

## The user reply channel {: #user-reply }

When cos emails you and you reply, that reply comes back in as a first-class message. Works from your phone because it's email — no vision dependency, no local hook.

```mermaid
sequenceDiagram
    participant Cos
    participant Mail as Gmail
    participant User
    participant Obs as email observer
    participant CosIn as cos (user_reply)

    Cos->>Mail: send_email_to_self (subject: [Deja] ...)
    Mail->>User: delivered
    User->>Mail: Reply "Re: [Deja] ..."
    Mail->>Obs: next poll
    Obs->>Obs: verify DMARC + From==user
    Obs->>Obs: dedupe Message-Id
    Obs->>CosIn: invoke_user_reply (subject, body, thread_id)
    CosIn->>Mail: send_email_to_self (threaded reply)
```

A couple of details matter:

- **Anti-spoofing is strict.** The parsed `From` must exactly match your authenticated Google identity, and Gmail's `Authentication-Results` header must show `dmarc=pass`. Rejections are audited.
- **Feedback loops are prevented.** Cos's outbound emails (subject starts with `[Deja]`, not `Re:`) are dropped by the observer. Only replies come back in.
- **Threading is preserved.** Cos replies with `in_reply_to` and `thread_id` set, so Gmail collapses the thread cleanly.

You can also reply by sending yourself an iMessage or WhatsApp message with `[Deja]` in the content. Same path — self-addressed, routed to `invoke_user_reply`, conversation logged under `~/Deja/conversations/`.

## Cos state on disk

The `chief_of_staff/` directory under `~/.deja/`:

| File | Purpose |
| ---- | ------- |
| `enabled` | Empty marker. Presence turns cos on. `deja cos enable/disable` toggles this. |
| `system_prompt.md` | Editable copy of the default cos system prompt. |
| `mcp_config.json` | MCP server config for the Claude subprocess. |
| `invocations.jsonl` | Log of every cos call — payload, return code, stdout, stderr. |
| `processed_replies` | One Message-Id per line; dedupes the email reply channel. |

`deja cos tail` streams the invocations log live. Very useful when you're watching cos make decisions in real time.

## When to add a new cos capability

The rule of thumb: if you're tempted to add a narrow LLM sweep somewhere — "let's have Flash classify these events as X or Y" — the right move is almost always:

1. Add a **deterministic candidate generator** (analyst layer).
2. Expose it as an **MCP tool** so cos can decide.

Integrate is the exception: it's immediate, bounded, and its prompt is load-bearing. Reflect is explicitly designed around cos-makes-the-call. New judgment logic almost always belongs on cos's side of the fence.
