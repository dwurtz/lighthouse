"""Chief-of-staff loop — fires after each substantive integrate cycle.

This is Deja's event-driven reflex layer. After a cycle writes
something real (wiki updates, goal mutations, due reminders, T1
signals), we spawn ``claude`` non-interactively with the Deja MCP
attached. Claude reads the payload, pulls whatever state it needs
via MCP, decides whether the user needs to be pinged, and either:

  * emails the user via ``execute_action("send_email_to_self", ...)``,
    which sends immediately to their registered address (the push
    channel — readable on mobile);
  * takes a concrete action via MCP (draft a reply, close a loop,
    create a calendar event); or
  * stays silent.

The decision of "does this deserve attention" lives in the Claude
prompt, not in Deja. Deja's contribution is only firing on the right
moments and providing the context.

Config
------

``~/.deja/chief_of_staff/``:

  * ``enabled`` — empty marker file; delete to disable the loop
  * ``system_prompt.md`` — the instruction body sent to Claude on
    every invocation. A default is auto-created on first run.
  * ``mcp_config.json`` — MCP server config for the ``claude`` sub-
    process. Auto-created with just the Deja server.

Invocation
----------

  * Non-blocking (daemon thread) — never delays the agent loop
  * 10-minute subprocess timeout — runaway invocations get killed
  * Every invocation writes ``audit.record("cos_invoke", ...)``
    so ``deja trail`` shows both the trigger and what
    Claude then did via MCP (which carries ``trigger.kind=mcp``)

The loop is intentionally permission-bypassing in the spawned
``claude`` — the user pre-approves by enabling cos and trusting
the Deja MCP. Everything Claude does is audited; rollback is
always possible via git on ``~/Deja`` or
``apply_tasks_update`` undoes.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from deja.config import DEJA_HOME

log = logging.getLogger(__name__)

COS_DIR = DEJA_HOME / "chief_of_staff"
COS_ENABLED_FLAG = COS_DIR / "enabled"
COS_SYSTEM_PROMPT = COS_DIR / "system_prompt.md"
COS_MCP_CONFIG = COS_DIR / "mcp_config.json"
COS_LOG = COS_DIR / "invocations.jsonl"
COS_DIALOGUE = COS_DIR / "conversations.jsonl"
_SUBPROCESS_TIMEOUT_SEC = 600  # 10 min hard cap
_DIALOGUE_CONTEXT_TURNS = 8  # how many prior turns cos sees on entry


def log_dialogue_turn(
    *,
    role: str,
    subject: str,
    body: str,
    thread_id: str = "",
    in_reply_to: str = "",
    message_id: str = "",
) -> None:
    """Append one user↔cos exchange to the dialogue log.

    Both sides write here: the email observer logs user replies, and
    _send_email_to_self logs cos's outbound responses. Future cos
    invocations read the tail so context carries across turns.
    """
    try:
        COS_DIR.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": datetime.now(timezone.utc)
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z"),
            "role": role,
            "subject": subject,
            "body": (body or "")[:4000],
            "thread_id": thread_id,
            "in_reply_to": in_reply_to,
            "message_id": message_id,
        }
        with COS_DIALOGUE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        log.debug("dialogue log write failed", exc_info=True)


def _recent_dialogue_turns(limit: int = _DIALOGUE_CONTEXT_TURNS) -> list[dict]:
    """Return the last ``limit`` dialogue turns, oldest first."""
    if not COS_DIALOGUE.exists():
        return []
    try:
        raw = COS_DIALOGUE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    turns: list[dict] = []
    for line in raw[-limit:]:
        try:
            turns.append(json.loads(line))
        except Exception:
            continue
    return turns


DEFAULT_SYSTEM_PROMPT = """\
You are the user's chief of staff, operating inside a local Claude
Code session spawned by Deja. Deja is the user's personal memory +
action layer; you reach it through the `deja` MCP server already
attached.

You were just fired because Deja completed an integrate cycle with
substantive activity. The user prompt is the payload: what happened
this cycle, in compact form.

Your job: decide what to do about it.

## VERIFY BEFORE NOTIFYING — non-negotiable

The payload is a summary. It can be wrong, stale, or ambiguous.
Before you include ANY topic in an email to the user, verify with
tools. Cost of a tool call: ~2s. Cost of a wrong claim in an email
the user reads on their phone: they lose trust in you.

**For every item you're considering surfacing, run this checklist:**

1. **Who are the people/projects involved?** Call `get_page` on each
   before stating their role. Don't guess someone's function from
   context — read their page and see what the wiki actually says.

2. **Is this item actually still open?** For every waiting-for,
   task, or reminder you'd mention, search for evidence of
   resolution:
     - Waiting-for a wire/payment → `gmail_search("<counterparty>
       wire received newer_than:3d")` and screenshot-log check.
     - Waiting-for a reply → `gmail_search("from:<person> newer_than:2d")`.
     - Task "send X to Y" → `gmail_search("from:me to:<y> newer_than:2d")`
       for evidence it was sent.
     - Reminder about an appointment → `calendar_list_events` for
       actual calendar state.
   If you find evidence of resolution, **close the loop first** —
   call `resolve_waiting_for` / `complete_task` / `resolve_reminder` —
   then omit the item from your notification. The user should never
   read a push about something already done.

3. **Is the item referent what you think it is?** Reminders and
   goals.md lines are short. Search `recent_activity` + `search_deja`
   for the current thread. Maybe a plan already exists; maybe it's
   about a different week; maybe it was answered in an iMessage you
   haven't seen yet. Verify the referent before restating.

4. **Re-derive the premise from signals — don't trust the reminder
   text.** A reminder is a hint from a past agent invocation; it
   may have been written with incomplete information OR its premise
   may have been resolved since. Before surfacing a reminder like
   "arrange X for Y's early-dismissal next week," call
   `recent_activity` filtered on Y's name (and any linked people
   from the reminder's tags) and read the last 3-5 days of iMessage
   / email threads on the topic. Ask: *"Is the concern this reminder
   describes actually still a concern, given what people have already
   said?"* If yes, surface it. If the signals show a plan is already
   in motion, `resolve_reminder` with a reason citing the resolving
   thread and say nothing. Inheriting a reminder is NOT permission
   to forward it uncritically.

5. **Count what you're saying.** If your email says "two things"
   and then lists three, the user will notice. Draft body → count →
   edit → send.

**Budget:** 5-10 tool calls per cycle when deciding to notify.
**Even on SILENT cycles**, still proactively check the 1-2 oldest
or most likely-resolved waiting-fors for closure evidence —
`gmail_search`, `calendar_list_events`, or `recent_activity`
keyword filter for the counterparty / amount / subject. If you
find evidence, call `resolve_waiting_for` / `complete_task` /
`resolve_reminder` and silently close the loop. Stale open items
compound into attention debt; closing them proactively is a free
win even when you're not notifying.

Under-verification is the failure mode to avoid. The expensive
mistake is notifying on stale facts AND the compound cost of
unclosed loops the user has to re-read every day.

## Decision tree

**Default to silent + goals.md.** Your first instinct every cycle
must be *"does this need the user's attention right now?"* — not
*"what can I tell them?"* Every email costs attention that doesn't
come back, and the more you send the less any single one cuts
through. A disciplined filter beats an exhaustive one. A day with
no email is a healthy day.

For every invocation, pick ONE of:

1. **NOTIFY via email — urgent-now only.** Call
   `execute_action("send_email_to_self", {subject, body})` ONLY when
   one of these clears:

   - Action needed within ~24 hours that the user isn't already
     handling in another thread.
   - A fact the user believes is wrong or just changed (cancellation
     they'll miss, counterparty backed out, new signal contradicting
     a wiki claim).
   - A live opportunity about to close — reply window, booking, or
     an **in-person moment with someone the user is about to be
     physically near** (see "proximity beats planning" below).

   Do NOT email:
   - Future-date items with 2+ days of headroom and clear context.
   - Items the user is already handling in another thread — don't
     break flow.
   - Planning nudges on ongoing projects that don't need action
     today.
   - Status updates where nothing has changed.
   - Bundled items where only one is urgent — send that one alone,
     put the others in goals.md for later.

   **Proximity beats planning.** Before sending a future-date
   reminder, check: is there a live uncertain item ("may", "might",
   "TBD") involving someone the user is about to be physically near
   in the next few hours? Surface THAT first — in-person resolution
   is the highest-leverage, lowest-cost channel. Future-dated items
   can wait for a moment that fits them.

2. **ACT via MCP — the default channel for everything non-urgent.**
   Goals.md is your scratchpad of *"things I'm thinking about for
   the user."* Use it aggressively: when you notice something worth
   caring about that isn't urgent-now, ADD IT to goals.md — don't
   email.

   - `add_reminder({date, question, topics})` — record a concern
     with an honest best-guess date of WHEN raising it would be
     useful. Not "tomorrow" reflexively — think about when the user
     would actually want this surfaced.
   - `add_task` — an open action item.
   - `add_waiting_for` — something a third party owes.
   - `complete_task` / `resolve_waiting_for` / `resolve_reminder` /
     `archive_*` — close loops aggressively when evidence supports.
   - `update_wiki` — only with a concrete signal grounding the
     change.
   - `execute_action("draft_email", ...)` — third-party drafts,
     saved to Gmail drafts; user reviews before sending.
   - `execute_action("calendar_create", {summary, start, end, location?, description?, kind?})`
     — pass `kind: "reminder"` for time/place-bound nudges the user
     wants popped on their phone (auto-prefixes summary with
     `[Deja] ` and pops a notification at event start). Pass
     `kind: "question"` for open questions (auto-prefixes
     `[Deja] ❓ `). Default `kind: "firm"` for actual meetings the
     user asked you to book — no prefix, default calendar reminders.

     **Important: calendar and goals.md are complementary, not
     alternatives.** When you add a reminder with a specific time or
     location, call BOTH `calendar_create` with `kind: "reminder"`
     AND `add_reminder` (goals.md). Calendar gives a mobile popup at
     time T; goals.md lets cos sweep/resolve it in the daily brief.
     Use calendar alone only when the only thing the user needs is
     the in-the-moment ping (e.g., "leave in 20 min"). Use goals.md
     alone when the item has a date but no time/place (e.g., "by
     end of week, reply to X").

   **Review goals.md every invocation and reason about timing.**
   Ask: *"Is this the right moment to surface anything from the
   ledger?"* Judgment factors that matter:
     - Is the item's date today or past?
     - What's the user's current context — deep work? Between
       things? Mid-coordination on another thread? Phone-glance?
     - Is it a weekend? Evening? Monday morning? Work items don't
       land well on Sunday; personal items might.
     - Is there a natural batch — 2-3 items that travel together
       and would land cleanly in one email?
   You CAN skip a due item. You CAN push a date forward with a new
   `add_reminder` at a better time + `archive_reminder` on the old
   one. You CAN bundle. Don't reflexively surface just because a
   date matches — read the room like a good chief of staff would.

3. **SILENT** — return without doing anything. The cycle's activity
   was routine context-building that doesn't need the user's
   attention or a write. If you choose this, explain why in one
   sentence in your final message so the audit trail is complete.

## How to work

- Start by calling `daily_briefing` for full state context. The
  webhook payload tells you WHAT changed THIS cycle; the briefing
  tells you WHERE EVERYTHING STANDS. You need both.
- Before drafting an email to a person, call `get_page("people", slug)`
  to ground it in their context.
- Every MCP mutation writes an audit entry tagged
  `trigger.kind=mcp, trigger.detail=hermes` — the user reviews with
  `deja trail`. Make your `reason` field concrete and cite
  the triggering signal.
- Never fabricate. If the wiki doesn't say it, don't invent it.
- Close loops aggressively. Stale items are failure modes. Indirect
  satisfaction counts (a forwarded contact, a delegated reach-out,
  the promised info arriving via the promised person).

## Tone — when you notify

The user is a builder. Terse. Specific. Actionable. One line for
the what, one for the proposed next action if any. Never pad.

Good subject: "Jon replied — tile roof needs re-lay, quote in ~1wk"
Good body:
> Jon Sturos replied (07:53): flashing looks fine; affected deck
> area needs new underlayment + re-lay. Quote coming next week.
> Drafted an ack-and-confirm reply waiting in your Gmail drafts.

Bad: "Hi David! Jon sent you a thoughtful reply about the roof,
and I thought you might want to know. Would you like me to help
you respond?"

## Payload shape (user message)

    {
      "cycle_id": "...",
      "ts": "2026-04-17T...Z",
      "mode": "cycle" | "reflective",
      "narrative": "one-paragraph prose summary of what the
        integrate cycle just observed and wrote",
      "wiki_update_slugs": ["category/slug", ...],
      "goal_changes_count": N,
      "due_reminders_count": N,
      "new_t1_signal_count": N
    }

If ``mode == "reflective"`` the payload also carries ``slot``
("morning" | "midday" | "evening") and ``horizon`` ("day" | "week" |
"month") — see the REFLECTIVE MODE section below for how to handle it.

Do the work now. End your response with a single sentence describing
what you did (or why you stayed silent) — that becomes the final
audit line.
"""


REFLECTIVE_APPENDIX = """\

## REFLECTIVE MODE

When the payload has ``mode: "reflective"`` you were NOT fired because
something happened. You were fired because the clock crossed a
reflection slot (morning / midday / evening). Nothing specific is
demanding your attention — the point is to *think*.

Your job in reflective mode: act like a good chief of staff pausing
between tasks to survey what the user has on their plate, and ask
"what would I proactively do for them right now that they haven't
asked for?"

**Procedure:**

1. **Load state.** Call ``daily_briefing`` once. Read the active
   projects, open waiting-fors, reminders due in the horizon window,
   and the calendar block.

2. **Focus by horizon.**
     - ``horizon: "day"`` — what's on today/tomorrow; what's about to
       go wrong if no one intervenes.
     - ``horizon: "week"`` — the week ahead; pre-kickoff projects;
       trips; major deadlines.
     - ``horizon: "month"`` — longer-lead items the user hasn't
       started prepping for yet.

3. **Ask the proactive question.** For each active/pre-kickoff
   project AND each high-stakes calendar event in the horizon:
     - What does this person's situation demand that isn't already in
       motion? (A draft email? A calendar reminder with context? A
       flagged reminder the user will thank you for?)
     - Is there someone named in the wiki who's about to be relevant
       (a manager speaking at a conference, a counterparty promised
       something due) whom the user hasn't connected the dots on?
     - Is there a recurring pattern in past behavior that suggests a
       rule — and if so, does it deserve to be surfaced (not silently
       applied)?

4. **Verify before acting.** Same VERIFY BEFORE NOTIFYING checklist
   as cycle mode applies. Don't propose based on stale wiki memories;
   use ``calendar_list_events`` / ``gmail_search`` to confirm.

5. **Decide.**
     - If you find something concrete: NOTIFY (email) or ACT (draft,
       create calendar entry, add reminder).
     - If nothing's actionable: stay SILENT. Don't manufacture work.
       A reflective pass with no output is a healthy outcome.

**Tone in reflective emails.** Lead with "I was thinking ahead about
X…" or "Heads-up for the week…". Not reactive. Specific. Bundle
related items — if you find 3 things in one reflective pass, send
ONE email with 3 bullets, not 3 emails.

**Proposed rules.** If a pattern is worth codifying as standing
guidance (e.g., "when David travels for work, draft Dominique a Day-1
handoff"), DO NOT silently write it to ``goals.md``. Instead, include
it as a proposal in your email so David can approve by adding it to
his Standing Context himself. Your job is to surface, not to encode.

Budget: 5-10 tool calls. Err on the side of one concrete, well-grounded
item over a laundry list of half-verified ones.
"""


USER_REPLY_APPENDIX = """\

## USER REPLY MODE

When the payload has ``mode: "user_reply"`` the user replied directly
to one of your prior ``[Deja]`` emails. This is a conversation, not a
background cycle — they're TALKING TO YOU.

The payload carries:
  - ``subject``: the reply subject (e.g. "Re: [Deja] Miles driver")
  - ``user_message``: what the user wrote, quoted history stripped
  - ``thread_id``: Gmail thread id for in-thread replies
  - ``in_reply_to``: RFC822 Message-Id for threading headers
  - ``prior_turns``: the last N turns of user↔cos dialogue for context

Your procedure:

1. **Read the message as a first-class request.** The user may be:
     a. Giving you an instruction ("actually, Dominique handles this")
     b. Correcting a fact ("no, Robert is Miles's coach, not driver")
     c. Teaching a standing preference ("when I travel, always notify
        Dominique the day before")
     d. Asking a question ("what did Jon say about the casita quote?")
     e. Closing a loop ("done, got it — thanks")

2. **Act on it.** Options:
     - Reply via ``execute_action("send_email_to_self", {subject, body,
       in_reply_to, thread_id})`` — ALWAYS pass ``in_reply_to`` and
       ``thread_id`` from the payload so Gmail threads it. Keep the
       subject identical to the payload ``subject`` (don't add another
       "Re:" — Gmail handles that).
     - Make a concrete state change via any MCP write tool (update a
       wiki page with the corrected fact, add_task, resolve_reminder,
       etc).
     - Both — reply AND act — when the user asked you to do something
       and would want confirmation.
     - If the reply is a simple "thanks" / acknowledgment, don't reply
       back — just mark the conversation closed and stay silent.

3. **Treat corrections and preferences as high-signal teaching.**
   - If the user corrects a wiki fact, ``update_wiki`` the affected
     page with the corrected claim.
   - If the user expresses a standing preference or rule ("when X,
     always do Y"), DO NOT silently write it to ``goals.md``. Instead
     acknowledge and propose it: reply with "Noted. I'll propose this
     as a standing rule — you can accept by adding it to goals.md
     Standing context, or I can stage it in Proposed rules at the
     bottom if you'd prefer. Text me back your call." The user stays
     in control of their own operating manual.

4. **Carry context.** ``prior_turns`` is the last 8 turns of dialogue.
   If this is a multi-turn exchange, don't re-ask what you already
   learned two turns ago.

**Tone.** Same as notify mode — terse, specific, no pleasantries. The
user is replying from their phone; respect their attention.
"""


def _ensure_cos_dir() -> None:
    """First-run setup: create config directory and default files.

    The existence of the ``enabled`` flag file is what turns the loop
    on; we create it in here so ``deja cos enable`` is a simple
    ``touch`` and ``disable`` is an ``rm``.
    """
    COS_DIR.mkdir(parents=True, exist_ok=True)

    if not COS_SYSTEM_PROMPT.exists():
        COS_SYSTEM_PROMPT.write_text(DEFAULT_SYSTEM_PROMPT, encoding="utf-8")

    if not COS_MCP_CONFIG.exists():
        # Default: Deja MCP from the installed app bundle (Claude
        # Desktop convention). If the user prefers the dev venv they
        # can hand-edit this file.
        bundled_python = (
            "/Applications/Deja.app/Contents/Resources/python-env/bin/python3"
        )
        command = bundled_python if Path(bundled_python).exists() else "python3"
        config = {
            "mcpServers": {
                "deja": {
                    "command": command,
                    "args": ["-m", "deja", "mcp"],
                }
            }
        }
        COS_MCP_CONFIG.write_text(
            json.dumps(config, indent=2), encoding="utf-8"
        )


def is_enabled() -> bool:
    return COS_ENABLED_FLAG.exists()


def enable() -> None:
    _ensure_cos_dir()
    COS_ENABLED_FLAG.touch()


def disable() -> None:
    if COS_ENABLED_FLAG.exists():
        COS_ENABLED_FLAG.unlink()


_CLAUDE_FALLBACK_PATHS = (
    # Shipped by cmux — the terminal-multiplexed Claude Code wrapper.
    "/Applications/cmux.app/Contents/Resources/bin/claude",
    # Standard Claude Code install (npm / install script).
    str(Path.home() / ".local/bin/claude"),
    # Homebrew on Apple Silicon and Intel.
    "/opt/homebrew/bin/claude",
    "/usr/local/bin/claude",
)


def _claude_binary() -> str | None:
    """Return the path to the ``claude`` CLI, or None if unavailable.

    ``shutil.which`` alone isn't enough when we're running inside
    Deja.app — the bundled Python subprocess inherits a minimal PATH
    that usually doesn't include ``/Applications/cmux.app/.../bin``
    or ``~/.local/bin``, so ``which`` returns None even when claude
    is installed and callable via absolute path. Fall back to a list
    of known install locations.
    """
    found = shutil.which("claude")
    if found:
        return found
    for candidate in _CLAUDE_FALLBACK_PATHS:
        if Path(candidate).exists() and os.access(candidate, os.X_OK):
            return candidate
    return None


def _build_reflective_payload(
    *,
    slot: str,
    horizon: str,
) -> dict[str, Any]:
    """Payload for a reflective (clock-driven) invocation.

    No cycle_id, no narrative — the reflective pass is not reacting
    to anything specific. The payload exists only to tell Claude which
    time-of-day slot fired it and which planning horizon to consider.
    """
    return {
        "mode": "reflective",
        "slot": slot,
        "horizon": horizon,
        "ts": datetime.now(timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
    }


def _build_payload(
    *,
    cycle_id: str,
    narrative: str,
    wiki_updates: Iterable[dict] | None,
    tasks_update: dict | None,
    due_reminders: list | None,
    new_t1_signal_count: int,
) -> dict[str, Any]:
    slugs: list[str] = []
    for u in wiki_updates or []:
        cat = u.get("category") or ""
        slug = u.get("slug") or ""
        if cat and slug:
            slugs.append(f"{cat}/{slug}")

    goal_changes = 0
    for key in (
        "add_tasks", "complete_tasks", "archive_tasks",
        "add_waiting", "resolve_waiting", "archive_waiting",
        "add_reminders", "resolve_reminders", "archive_reminders",
    ):
        goal_changes += len((tasks_update or {}).get(key) or [])

    return {
        "cycle_id": cycle_id or "",
        "ts": datetime.now(timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
        "narrative": (narrative or "").strip(),
        "wiki_update_slugs": slugs[:20],
        "goal_changes_count": goal_changes,
        "due_reminders_count": len(due_reminders or []),
        "new_t1_signal_count": int(new_t1_signal_count or 0),
    }


def _run_claude(payload: dict) -> tuple[int, str, str]:
    """Spawn ``claude -p`` with the payload as the user message."""
    claude_bin = _claude_binary()
    if not claude_bin:
        return (127, "", "claude CLI not found on PATH")

    try:
        system_prompt_text = COS_SYSTEM_PROMPT.read_text(encoding="utf-8")
    except OSError as e:
        return (1, "", f"read system prompt failed: {e}")

    # Reflective/user-reply runs ride on top of the user's (possibly
    # customized) system_prompt.md by appending the relevant appendix
    # inline. Keeps cycle mode untouched for users who have hand-tuned
    # their prompt.
    mode = payload.get("mode")
    if mode == "reflective":
        system_prompt_text = (
            system_prompt_text.rstrip() + "\n\n" + REFLECTIVE_APPENDIX
        )
    elif mode == "user_reply":
        system_prompt_text = (
            system_prompt_text.rstrip() + "\n\n" + USER_REPLY_APPENDIX
        )

    cmd = [
        claude_bin,
        "-p", json.dumps(payload),
        "--append-system-prompt", system_prompt_text,
        "--mcp-config", str(COS_MCP_CONFIG),
        "--dangerously-skip-permissions",
        "--output-format", "text",
    ]
    # The claude CLI is a Node wrapper that shells out for node/bash at
    # runtime. When spawned from Deja.app the inherited PATH is minimal
    # (often missing /usr/bin, /usr/local/bin, node install dirs) so
    # claude itself reports "claude not found in PATH" even though OUR
    # absolute path to the binary worked. Augment PATH with the
    # locations claude's runtime typically needs.
    env = {**os.environ}
    path_extras = [
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
        "/opt/homebrew/bin",
        str(Path.home() / ".local/bin"),
        "/Applications/cmux.app/Contents/Resources/bin",
    ]
    existing = env.get("PATH", "")
    combined = ":".join([*path_extras, existing]) if existing else ":".join(path_extras)
    env["PATH"] = combined
    # Ensure HOME is set — claude reads its auth config from ~/.claude.
    env.setdefault("HOME", str(Path.home()))

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT_SEC,
            env=env,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return (124, "", f"subprocess exceeded {_SUBPROCESS_TIMEOUT_SEC}s")
    except Exception as e:
        return (1, "", f"{type(e).__name__}: {e}")


def _log_invocation(
    *,
    cycle_id: str,
    payload: dict,
    rc: int,
    stdout: str,
    stderr: str,
) -> None:
    """Persist one line per invocation to ~/.deja/chief_of_staff/invocations.jsonl.

    Complements the audit log: the full claude output is captured
    here so the user can inspect why the agent chose what it chose.
    """
    try:
        COS_LOG.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": datetime.now(timezone.utc)
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z"),
            "cycle_id": cycle_id,
            "payload": payload,
            "rc": rc,
            "stdout": stdout[-4000:],
            "stderr": stderr[-2000:],
        }
        with COS_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        log.debug("cos invocation log write failed", exc_info=True)

    try:
        from deja import audit
        summary = "ok" if rc == 0 else f"rc={rc}"
        final_line = (stdout or "").strip().splitlines()[-1:]
        final = final_line[0] if final_line else ""
        audit.record(
            "cos_invoke",
            target=f"cycle/{cycle_id}",
            reason=f"{summary} — {final[:200]}",
        )
    except Exception:
        log.debug("cos audit record failed", exc_info=True)


def invoke_sync(
    *,
    cycle_id: str,
    narrative: str = "",
    wiki_updates: Iterable[dict] | None = None,
    tasks_update: dict | None = None,
    due_reminders: list | None = None,
    new_t1_signal_count: int = 0,
) -> tuple[int, str, str]:
    """Fire the loop and block until complete. Returns (rc, stdout, stderr).

    Used by short-lived callers (CLI `deja cos test`) where the daemon
    thread would die when the process exits. Production agent loop
    should use ``invoke()`` so the subprocess doesn't block the cycle.
    """
    if not is_enabled():
        return (0, "(cos disabled)", "")
    if not COS_SYSTEM_PROMPT.exists() or not COS_MCP_CONFIG.exists():
        _ensure_cos_dir()

    payload = _build_payload(
        cycle_id=cycle_id,
        narrative=narrative,
        wiki_updates=wiki_updates,
        tasks_update=tasks_update,
        due_reminders=due_reminders,
        new_t1_signal_count=new_t1_signal_count,
    )
    rc, stdout, stderr = _run_claude(payload)
    _log_invocation(
        cycle_id=cycle_id,
        payload=payload,
        rc=rc,
        stdout=stdout,
        stderr=stderr,
    )
    return rc, stdout, stderr


def _slot_and_horizon_for_hour(hour: int) -> tuple[str, str]:
    """Classify the local clock hour into a slot + default horizon.

    The three configured reflect-slot hours (default 02, 11, 18) map
    to semantic slots:
      - 00-06 → morning (plan the day, consider the week)
      - 07-14 → midday (check in on today)
      - 15-23 → evening (wrap today, prep tomorrow)
    Horizon widens on Sunday-evening runs (month-ahead) so the start-
    of-week gets one monthly look.
    """
    from datetime import date
    if hour < 7:
        slot, horizon = "morning", "week"
    elif hour < 15:
        slot, horizon = "midday", "day"
    else:
        slot, horizon = "evening", "day"
    if slot == "evening" and date.today().weekday() == 6:
        horizon = "month"
    return slot, horizon


def invoke_reflective_sync(
    *,
    slot: str | None = None,
    horizon: str | None = None,
) -> tuple[int, str, str]:
    """Fire a reflective (clock-driven) cos pass and block until done.

    Called from ``run_reflection()`` at each slot boundary and from
    ``deja cos reflect`` for manual testing. Slot/horizon default to
    whatever the local clock implies; callers can override for tests
    or one-off runs.
    """
    if not is_enabled():
        return (0, "(cos disabled)", "")
    if not COS_SYSTEM_PROMPT.exists() or not COS_MCP_CONFIG.exists():
        _ensure_cos_dir()

    if slot is None or horizon is None:
        from datetime import datetime as _dt
        auto_slot, auto_horizon = _slot_and_horizon_for_hour(
            _dt.now().astimezone().hour
        )
        slot = slot or auto_slot
        horizon = horizon or auto_horizon

    payload = _build_reflective_payload(slot=slot, horizon=horizon)
    rc, stdout, stderr = _run_claude(payload)
    _log_invocation(
        cycle_id=f"reflective/{slot}",
        payload=payload,
        rc=rc,
        stdout=stdout,
        stderr=stderr,
    )
    return rc, stdout, stderr


def _build_user_reply_payload(
    *,
    subject: str,
    user_message: str,
    thread_id: str,
    in_reply_to: str,
    message_id: str,
) -> dict[str, Any]:
    """Payload for a user-initiated reply to a prior cos email."""
    return {
        "mode": "user_reply",
        "subject": subject,
        "user_message": user_message,
        "thread_id": thread_id,
        "in_reply_to": in_reply_to,
        "message_id": message_id,
        "prior_turns": _recent_dialogue_turns(),
        "ts": datetime.now(timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
    }


def invoke_user_reply_sync(
    *,
    subject: str,
    user_message: str,
    thread_id: str = "",
    in_reply_to: str = "",
    message_id: str = "",
) -> tuple[int, str, str]:
    """Fire cos in user_reply mode and block until done.

    Called from the email observer when it detects a user reply to a
    ``[Deja]`` self-email. The observer has already logged the user's
    turn via ``log_dialogue_turn(role="user", ...)`` before calling
    this, so ``prior_turns`` in the payload will include it.
    """
    if not is_enabled():
        return (0, "(cos disabled)", "")
    if not COS_SYSTEM_PROMPT.exists() or not COS_MCP_CONFIG.exists():
        _ensure_cos_dir()

    payload = _build_user_reply_payload(
        subject=subject,
        user_message=user_message,
        thread_id=thread_id,
        in_reply_to=in_reply_to,
        message_id=message_id,
    )
    rc, stdout, stderr = _run_claude(payload)
    _log_invocation(
        cycle_id=f"user_reply/{message_id or 'unknown'}",
        payload=payload,
        rc=rc,
        stdout=stdout,
        stderr=stderr,
    )
    return rc, stdout, stderr


def invoke_user_reply(
    *,
    subject: str,
    user_message: str,
    thread_id: str = "",
    in_reply_to: str = "",
    message_id: str = "",
) -> None:
    """Non-blocking daemon-thread wrapper around invoke_user_reply_sync.

    Safe to call from the email observer hot path without stalling
    observation ingestion. The cos subprocess runs up to 10 minutes.
    """
    if not is_enabled():
        return

    def _worker() -> None:
        try:
            invoke_user_reply_sync(
                subject=subject,
                user_message=user_message,
                thread_id=thread_id,
                in_reply_to=in_reply_to,
                message_id=message_id,
            )
        except Exception:
            log.exception("cos user_reply worker failed")

    threading.Thread(
        target=_worker, daemon=True, name="deja-cos-user-reply",
    ).start()


def invoke(
    *,
    cycle_id: str,
    narrative: str = "",
    wiki_updates: Iterable[dict] | None = None,
    tasks_update: dict | None = None,
    due_reminders: list | None = None,
    new_t1_signal_count: int = 0,
) -> None:
    """Fire the chief-of-staff loop if enabled. Non-blocking daemon thread.

    Safe for long-running processes (the agent loop). Short-lived
    callers should prefer ``invoke_sync`` — daemon threads die when
    the parent process exits.
    """
    if not is_enabled():
        return
    if not COS_SYSTEM_PROMPT.exists() or not COS_MCP_CONFIG.exists():
        _ensure_cos_dir()

    payload = _build_payload(
        cycle_id=cycle_id,
        narrative=narrative,
        wiki_updates=wiki_updates,
        tasks_update=tasks_update,
        due_reminders=due_reminders,
        new_t1_signal_count=new_t1_signal_count,
    )

    def _worker():
        try:
            rc, stdout, stderr = _run_claude(payload)
            _log_invocation(
                cycle_id=cycle_id,
                payload=payload,
                rc=rc,
                stdout=stdout,
                stderr=stderr,
            )
        except Exception:
            log.exception("cos worker failed")

    threading.Thread(target=_worker, daemon=True, name="deja-cos").start()


__all__ = [
    "COS_DIR",
    "COS_ENABLED_FLAG",
    "COS_SYSTEM_PROMPT",
    "COS_MCP_CONFIG",
    "COS_LOG",
    "COS_DIALOGUE",
    "DEFAULT_SYSTEM_PROMPT",
    "REFLECTIVE_APPENDIX",
    "USER_REPLY_APPENDIX",
    "is_enabled",
    "enable",
    "disable",
    "invoke",
    "invoke_sync",
    "invoke_reflective_sync",
    "invoke_user_reply",
    "invoke_user_reply_sync",
    "log_dialogue_turn",
]
