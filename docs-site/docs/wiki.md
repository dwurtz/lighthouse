# The wiki

Deja's memory is a folder of Markdown files, committed to a local git repo, browsable in Obsidian. That's it.

This choice carries most of the design. Every other decision — how cos reasons, how integrate writes, how the MCP tool surface looks — falls out of "the wiki is the source of truth and it has to stay legible to a human."

!!! info "The wiki is one of two on-disk roots"
    Deja's state lives in two separate directories, on purpose:

    - **`~/Deja/`** — the wiki (what this page is about). Git-tracked, human-readable, reviewable, reversible. Share it with a private remote if you want; treat it like a notebook you and an agent co-author.
    - **`~/.deja/`** — raw state. Observations log, audit log, screenshot PNGs, OCR sidecars, cos config, sockets, cursors. Not git-tracked (too big, too noisy, too private at the raw level). You could delete it tomorrow; the wiki here still carries everything Deja actually "knows."

    Integrate's job is to distill the raw stream into the wiki. If something matters enough to reason about twice, it ends up in `~/Deja/`. If it's just signal, it stays under `~/.deja/`.

## Layout

```text
~/Deja/
├── index.md                      # auto-generated catalog, sorted by mtime
├── goals.md                      # tasks, waiting-fors, reminders, standing context
├── log.md                        # human-readable activity log
├── reflection.md                 # daily synthesis (morning note)
├── people/<slug>.md              # one page per person
├── projects/<slug>.md            # one page per ongoing project
├── events/YYYY-MM-DD/<slug>.md   # timestamped events with YAML frontmatter
├── observations/YYYY-MM-DD.md    # daily narrative log
├── conversations/YYYY-MM-DD/<slug>.md   # per-thread user↔cos dialogues
├── prompts/                      # editable LLM prompts
└── .git/                         # every agent write is a commit
```

There are only three durable categories: **people**, **projects**, **events**. Everything else in the wiki is either an index (`index.md`, `log.md`), a ledger (`goals.md`), or session material (`conversations/`, `observations/`).

## The three categories

| Category | Describes | Example slug |
| -------- | --------- | ------------ |
| `people/` | A person — who they are, what's true about them now | `jane-pm` |
| `projects/` | A project, thread, or life arc — what it is, where it stands | `office-relocation` |
| `events/<date>/` | Something that happened — timestamped, entity-linked | `2026-04-18/jane-pm-accepted-offer` |

**Entity pages** (people, projects) are short prose — 100–400 words, present tense, leading with what's true right now. They're not a journal. When a new fact lands, the integrate LLM either updates the page body (if the fact is durable) or drops a line in the `## Recent` section with a wiki-link to the event page.

**Event pages** are the journal. They're short, they have YAML frontmatter for structured linking, and they get materialized from concrete signals (an email, a calendar event, an iMessage that resolves a loop). Example:

```yaml
---
date: 2026-04-18
time: "11:01"
people: [jane-pm, joe-partner]
projects: [office-relocation]
---

Jane confirmed the new Pier 39 address over iMessage. Lease starts May 1.
Joe is handling the move coordinator call tomorrow at 3pm.
```

You don't write that frontmatter by hand. The integrate LLM emits a structured `event_metadata` field and the wiki writer serializes it into YAML.

## Why Markdown and git

Because every AI system eventually gets something wrong, and when it does, you want to:

1. **See** what it wrote. Markdown is legible. A JSON blob in a database is not.
2. **Diff** it against what it knew before. Git does this for free.
3. **Revert** individual mistakes without losing good work. `git revert <sha>`.
4. **Trust** the local copy. Plain files are hard to lose. A corrupted SQLite file is catastrophic; a broken Markdown file is annoying.

There's a secondary benefit: Obsidian. You can open `~/Deja/` in Obsidian, edit pages by hand, follow `[[wiki-links]]`, and the next agent cycle sees your edits. The wiki is yours, not the agent's.

## `index.md` is load-bearing

After every wiki write, a pure-Python function rebuilds `index.md`: a flat list of every page, newest-first by mtime, with a one-line summary.

```mermaid
flowchart LR
    Writes["agent writes<br/>(create/update/delete)"] --> Rebuild[wiki_catalog.rebuild_index]
    Rebuild --> Idx["index.md<br/>(mtime-desc, one-liner per page)"]
    Idx --> R1["integrate's<br/>wiki_retriever"]
    Idx --> R2["triage prefilter<br/>(catalog-aware)"]
    Idx --> R3["vision prompt<br/>(truncated view)"]

    classDef op fill:#1a365d,stroke:#2c5282,color:#f7fafc
    classDef idx fill:#744210,stroke:#975a16,color:#fefcbf
    class Writes,Rebuild op
    class Idx,R1,R2,R3 idx
```

Three different consumers read `index.md` top-down within an attention budget. The ordering is not cosmetic — it directly decides what each downstream LLM sees first. If you touch a page, it jumps to the top, and the next integrate cycle sees it before older stuff. This is the cheapest and most effective recency signal in the system.

!!! tip "Don't 'normalize' this"
    If you read the code and think "I could sort `index.md` alphabetically," don't. The mtime ordering is load-bearing in three places.

## `goals.md` — the working ledger

This file deserves its own page — it's where cos does most of its thinking-across-time, and understanding it is load-bearing for understanding how cos decides when to surface things. See [**goals.md — the working ledger**](goals-file.md) for the full tour of all seven sections (Standing context, Automations, Tasks, Waiting for, Reminders, Archive, Recurring), who writes to each, and why this one file shapes most of cos's behavior.

## Rules the integrate LLM follows

The integrate prompt is ~200 lines. A few rules are load-bearing:

- **Only write what signals say.** Inference is fine; fabrication is not.
- **Deletion requires explicit user retraction.** Agents don't remove facts just because they're old.
- **Person pages require structured grounding.** A name in screenshot OCR alone isn't enough — there must be an email, phone, chat label, or an existing `[[slug]]` reference somewhere.
- **Update-without-new-fact is banned.** If there's no concrete new information, don't rewrite the page.
- **Durable facts get promoted to the entity body.** A one-off event ("Jane mentioned she's moving") becomes a standing fact ("Jane is relocating to Pier 39 office in May").

These rules exist because earlier versions of the agent had fun writing speculative prose. That made the wiki less trustworthy, which made cos's downstream decisions less trustworthy, which was exactly the wrong direction.

## The state directory

The wiki lives at `~/Deja/`. Deja's operational state lives separately at `~/.deja/` — observations, audit trail, PID files, sockets, buffers, configuration. It's not meant to be human-legible; it's where the pipelines checkpoint and buffer. A short tour:

| File | Purpose |
| ---- | ------- |
| `observations.jsonl` | Append-only log of every raw signal |
| `audit.jsonl` | Every state mutation with reason + trigger |
| `config.yaml` | User config — feature flags, slot hours, model choices |
| `last_integration_offset` | Byte offset into observations.jsonl; resumption mark |
| `chief_of_staff/` | Cos config + invocation log |
| `raw_ocr/<date>/<id>.txt` | Apple Vision OCR text for screenshots |
| `raw_images/<date>/<id>.png` | Raw screenshot PNGs |
| `deja.sock` | Unix socket FastAPI listens on |

You can poke at this stuff if you're debugging (`deja trail`, `deja status`), but the day-to-day surface is the wiki.

## The mental shortcut

If you remember one thing about storage: **the wiki is the input to every agent decision**, and every agent decision is a **commit** to the wiki. Everything else (observations, audit, buffers) supports that loop.
