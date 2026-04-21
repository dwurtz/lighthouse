# Glossary

A quick reference for the terms used throughout this site. Alphabetized; each definition is one or two sentences. Follow the links for the full page on any concept.

## audit

The append-only trail of every state mutation, kept at `~/.deja/audit.jsonl`. Rows carry the tool called, the reason, and the trigger (integrate, cos, or direct MCP). Audit entries surfaced in a standard format are tagged **Hermes** — that's the audit bus, not a separate agent. Inspect with `deja trail`.

## cos

The decision layer. A fresh [Claude CLI](cos.md) subprocess spawned via `claude -p` whenever Deja needs to think about something. Reads state through [MCP tools](mcp.md), writes state through [MCP tools](mcp.md). Fires in four modes: cycle (after substantive [integrate](pipelines.md#integrate)), reflective (clock slots), user_reply (self-addressed message in), command (notch voice or chat).

## goals.md

The working ledger at `~/Deja/goals.md`. A single markdown file with seven sections — Standing context, Automations, Tasks, Waiting for, Reminders, Archive, Recurring — that both you and cos read and write. See the full tour on [goals.md — the working ledger](goals-file.md).

## integrate

The 5-minute [pipeline](pipelines.md#integrate). Reads new observations since the last offset, pulls wiki context, calls Claude Opus with the batch, applies the JSON response as wiki writes plus goal and task updates, then fires cos if the cycle was substantive.

## MCP tool

A single entry in cos's tool surface, exposed by the `deja mcp` stdio server. Read tools let cos assemble context (`daily_briefing`, `search_deja`, `get_page`, `gmail_search`, etc.); write tools let cos mutate state (`update_wiki`, `add_task`, `execute_action`, `send_email_to_self`, etc.). See the full [MCP tool surface](mcp.md).

## observation

The persisted record of a [signal](#signal-vs-observation), written as one row in `~/.deja/observations.jsonl`. Observations are deduped by `id_key`, tier-labeled, and append-only. Integrate reads from this file.

## observe

The 3-second [pipeline](pipelines.md#observe). Polls each source ("anything new?"), dedupes by `id_key`, assigns a tier, appends to `observations.jsonl`. No LLM calls in the hot path.

## pipeline

One of the three loops in the monitor process: [observe](pipelines.md#observe) (every 3s), [integrate](pipelines.md#integrate) (every 5 min), [reflect](pipelines.md#reflect) (3x/day). They run on different cadences but share the same on-disk substrate, so a signal becomes a wiki update becomes a reflection candidate without any glue code.

## raw state

Everything under `~/.deja/` — the observation log, audit log, screenshot PNGs, OCR sidecars, caches, sockets, cos config. Not git-tracked. Fat, noisy, private at the raw level; you can delete it tomorrow and [the wiki](wiki.md) still carries everything Deja actually knows. See the [two-directories split](index.md#two-directories-one-discipline).

## reflect

The three-times-a-day [pipeline](pipelines.md#reflect) (02:00, 11:00, 18:00 local). Runs deterministic prep — refresh vector embeddings, precompute candidate sets, trim audit — then hands off to one cos reflective pass. Sleep-safe: a missed slot fires once on wake.

## signal (vs observation)

A **signal** is raw incoming data — an inbound iMessage, a sent email, a screenshot, a calendar delta. It becomes an **observation** once observe has deduped and tiered it and written it to `observations.jsonl`. The distinction matters: a signal is ephemeral; an observation is the persisted record integrate and cos can reason over. See [signal sources](signals.md) for the full source list.

## tier

The T1 / T2 / T3 label a deterministic function assigns to every observation. **T1** — user-authored or inbound from inner circle. **T2** — focused attention (active threads, close contacts, current calendar context). **T3** — ambient or background (automation mail, passive browsing, OCR noise). Integrate always keeps T1 and T2; T3-only batches get dropped. See [tiering](signals.md) for how it's applied per source.

## the wiki

The markdown memory at `~/Deja/` — one page per person, project, and event, plus `index.md`, `goals.md`, and daily observation narratives. Git-tracked; every agent write is a commit with a reason. Obsidian-browsable; human-legible. See [the wiki](wiki.md).
