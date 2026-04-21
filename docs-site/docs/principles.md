# Design principles

Deja has three design commitments, plus half a dozen practical rules that fall out of them. This page states them plainly so the rest of the system has a reference.

## Local-first

Raw [signals](signals.md) and [the wiki](wiki.md) stay on your machine. LLM calls go out to a lightweight proxy; nothing else.

That rules out a lot of otherwise reasonable designs:

- No telemetry. No analytics. No "help improve Deja" background pings.
- No server-side storage of your messages, screenshots, or calendar.
- No cross-user features. Deja is single-user by construction.

It also adds some friction:

- TCC permissions (Full Disk Access, Screen Recording, Accessibility, Microphone) have to be granted locally.
- Sync across machines is your problem. The wiki is a git repo — you can use any git remote you trust, or none.
- If an LLM endpoint is down, Deja keeps observing and buffering, but new thoughts pause until you're online.

Local-first is an **engineering constraint**, not a marketing slogan. It shapes the choice of storage (Markdown + git), the shape of the backend (Python subprocesses, not a cloud service), the auth model (OAuth tokens in Keychain, not a Deja account), and the upgrade model (Sparkle auto-update, not a server push).

## Trust over coverage

The worst thing Deja can do is tell you something confidently wrong while you're glancing at your phone.

The second-worst thing is cry wolf. If [cos](cos.md) sends three emails a day and two of them are irrelevant, you stop reading.

So the system is built around a disciplined filter:

- **Tiering drops noise before [integrate](pipelines.md#integrate).** T3-only message batches never reach the wiki.
- **Screenshot preprocess can return `None`.** A noisy frame is dropped entirely, not summarized into the wiki.
- **cos defaults to SILENT.** NOTIFY is reserved for three specific cases: action within 24h the user isn't handling; a fact the user believes is wrong or just changed; a live opportunity about to close.
- **cos writes to [`goals.md`](goals-file.md) instead of emailing.** Non-urgent thoughts go to the scratchpad and future cos cycles decide when (or whether) to surface them.
- **Integrate's prompt bans speculative writes.** If there's no concrete new fact, don't rewrite the page.

!!! quote "A day with no email from Deja is a healthy day."

The metric isn't activity. The metric is the quality of the handful of things that do bubble up.

## Git-backed

Every agent write is a commit. Every commit has a reason. Every mistake is reversible.

This is cheap to implement and pays for itself the first time the agent gets something wrong. The wiki is plain text — you can:

- `git log --oneline people/jane.md` — see every change to a page and why.
- `git show <sha>` — read the agent's reasoning at the time.
- `git revert <sha>` — undo a specific mistake without losing anything else.

Obsidian opens `~/Deja/` directly. You can edit by hand, and the next agent cycle reads your edits.

The git commitment also pays off in human trust. When something surprising lands in the wiki, you can see who wrote it and why.

## cos is the decision layer; analysts are cheap

The mental model has two pieces: **analysts** (cheap, deterministic, high-volume) and **cos** (one capable agent with tools).

Analysts do things like:

- Classify a signal as T1/T2/T3.
- Condense a screenshot's OCR into a structured block, or return None.
- Score 500 page pairs by cosine similarity and return the top 40.
- Cluster 300 events by shared entities.

They're bad at anything resembling judgment. If you use a cheap LLM for a judgment call — "is this page pair really the same person?" — you get false positives that damage the wiki. That's where earlier Flash-confirm sweeps failed.

cos does judgment. It runs rarely (a few dozen times a day), it has tools to verify before it writes, and it has an [MCP](mcp.md) surface to ask the user when it genuinely can't resolve something.

**Practical consequence:** when you're tempted to add a narrow LLM sweep, the right move is almost always:

1. Add a deterministic candidate generator.
2. Expose it as an MCP tool so cos can decide.

Integrate is the exception. It's immediate, bounded, and its prompt is load-bearing — 200 lines of rules that have been tuned over many cycles. New judgment logic belongs on cos's side of the fence.

## Silence is a legitimate output

Most invocations of cos return SILENT. Most integrate calls produce no wiki write. Most observations are T3 and get filtered.

This is the correct shape. A system that feels compelled to do something on every tick becomes an adversary. A system that's comfortable doing nothing most of the time becomes trustworthy.

Silence is also a testable output. `deja trail` shows what cos decided, including "silent — nothing to surface." If you're seeing frequent NOTIFY decisions for things that should be SILENT, something is wrong with the filter, not the model.

## Filter, don't plan

cos doesn't hold a plan about "today I will send a briefing at 7 AM and a closeout at 6 PM." Timing itself is a reasoning task.

Goals.md is cos's scratchpad. Every invocation, cos reads the scratchpad along with whatever else it needs, and decides — given the time of day, day of week, recent user activity, natural batching opportunities — whether to surface each item right now.

This means:

- A Friday afternoon thought can surface Monday morning.
- A Monday morning reminder can get suppressed if the user is clearly mid-coordination on something else.
- Two small items noticed across a day can batch into one evening email instead of two mid-day pings.

Fixed digest schedules are what notification systems do. Deja's filter is a reasoning layer.

## Write-through and legibility

Everything the agent writes is human-legible in the wiki. You can open `~/Deja/` in Obsidian, follow `[[slug]]` links by hand, edit pages, delete them, revert them.

This rules out "clever" storage:

- No vector DB as the source of truth. QMD indexes are caches; the wiki is canonical.
- No JSON blobs representing people. Each person is a paragraph of prose.
- No opaque agent scratch space. cos's thinking-over-time happens in `goals.md` and the wiki, both of which you can read.

Write-through to plain files is slightly more expensive. It's also the difference between an agent you can trust and one you can't.

## The rules, compressed

- **Local raw data.** Network is for LLM calls, nothing else.
- **Git is the audit log.** Commit every write with a reason.
- **Cheap analysts, capable decider.** Don't use a cheap LLM for a judgment call.
- **Silence is fine.** Most cycles produce nothing.
- **Filter, don't plan.** Timing is itself a reasoning task.
- **Write to the wiki, not a scratchpad.** Everything legible, everything reversible.

These aren't prescriptions for every AI project. They're the rules that fall out of "this agent reads my messages and talks to my calendar, so it had better be cheap to inspect and cheap to roll back."
