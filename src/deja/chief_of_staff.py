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
import re
import shutil
import subprocess
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import yaml

from deja.config import DEJA_HOME, WIKI_DIR

log = logging.getLogger(__name__)

COS_DIR = DEJA_HOME / "chief_of_staff"
COS_ENABLED_FLAG = COS_DIR / "enabled"
COS_SYSTEM_PROMPT = COS_DIR / "system_prompt.md"
COS_MCP_CONFIG = COS_DIR / "mcp_config.json"
COS_LOG = COS_DIR / "invocations.jsonl"
# Legacy single-file dialogue log. Superseded by per-conversation Markdown
# files under ``~/Deja/conversations/``; kept as a constant so the
# migration helper can find and rename it.
COS_DIALOGUE = COS_DIR / "conversations.jsonl"
CONVERSATIONS_DIR = WIKI_DIR / "conversations"
_SUBPROCESS_TIMEOUT_SEC = 600  # 10 min hard cap


# ---------------------------------------------------------------------------
# Conversation file helpers — per-thread Markdown pages under
# ``~/Deja/conversations/YYYY-MM-DD/<slug>.md``. Mirrors the events layout
# so the same QMD index + MCP tools (search_deja, get_page) surface
# conversation history alongside wiki pages.
# ---------------------------------------------------------------------------


_SUBJECT_SLUG_RE = re.compile(r"[^a-z0-9]+")
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


def _slugify_subject_hint(subject: str) -> str:
    """Derive a short human-readable slug hint from an email subject.

    Strips ``Re:`` prefixes and the ``[Deja]`` tag, lowercases, and
    collapses non-alphanumeric runs into ``-``. Truncates to 6 words so
    slugs stay filesystem-friendly. Empty subject → ``"conversation"``.
    """
    s = (subject or "").strip()
    while True:
        low = s.lower()
        if low.startswith("re:"):
            s = s[3:].strip()
            continue
        if low.startswith("fwd:") or low.startswith("fw:"):
            s = s.split(":", 1)[1].strip()
            continue
        break
    s = s.replace("[Deja]", "").replace("[deja]", "").strip()
    s = _SUBJECT_SLUG_RE.sub("-", s.lower()).strip("-")
    words = [w for w in s.split("-") if w]
    if not words:
        return "conversation"
    return "-".join(words[:6])


def _conversation_slug(subject: str, thread_id: str) -> str:
    """Deterministic slug: ``<subject-hint>--thread-<first-4-hex>``.

    Same ``thread_id`` + ``subject`` always produces the same slug so
    appends to a thread land in the same file. Threads without an id
    fall back to a date-stamped hint so each one-off message still gets
    its own file.
    """
    hint = _slugify_subject_hint(subject)
    tid = (thread_id or "").strip().lower()
    if tid:
        short = re.sub(r"[^0-9a-f]", "", tid)[:4] or "xxxx"
        return f"{hint}--thread-{short}"
    # No thread id — stable on subject alone, tagged with date so a
    # later ad-hoc turn with the same subject doesn't collide.
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    return f"{hint}--solo-{stamp}"


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split ``text`` into (parsed_frontmatter_dict, body). Tolerant."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    return meta, text[m.end():]


def _serialize_frontmatter(meta: dict) -> str:
    """Stable, human-editable YAML for the conversation header."""
    lines = ["---"]
    if meta.get("thread_id"):
        lines.append(f'thread_id: "{meta["thread_id"]}"')
    if meta.get("subject"):
        subj = str(meta["subject"]).replace('"', '\\"')
        lines.append(f'subject: "{subj}"')
    participants = meta.get("participants") or []
    if participants:
        lines.append("participants: [" + ", ".join(participants) + "]")
    lines.append(f'channel: {meta.get("channel", "email")}')
    if meta.get("started_at"):
        lines.append(f'started_at: "{meta["started_at"]}"')
    if meta.get("updated_at"):
        lines.append(f'updated_at: "{meta["updated_at"]}"')
    lines.append("---")
    return "\n".join(lines)


def _atomic_write(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` via tempfile + rename (same dir)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _find_existing_conversation(slug: str) -> Path | None:
    """Look up an existing conversation file by slug across all date dirs.

    Conversations live under ``conversations/<date>/<slug>.md``. The
    ``<date>`` is the thread's start date — unknown to later turns.
    Walks the dir newest-first and returns the first match.
    """
    if not CONVERSATIONS_DIR.exists():
        return None
    try:
        subdirs = sorted(
            (p for p in CONVERSATIONS_DIR.iterdir() if p.is_dir()),
            reverse=True,
        )
    except OSError:
        return None
    target = f"{slug}.md"
    for d in subdirs:
        candidate = d / target
        if candidate.exists():
            return candidate
    return None


def _role_label(role: str) -> str:
    """Normalize role keys to the heading labels we write into files."""
    r = (role or "").strip().lower()
    if r in ("user", "human", "david"):
        return "user"
    if r in ("cos", "assistant", "deja", "deja-cos"):
        return "cos"
    return r or "unknown"


def _now_iso_utc() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _now_local_heading() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")


def _format_turn_section(role: str, body: str) -> str:
    """One turn = ``## <role> — <local timestamp>`` + body."""
    return f"## {_role_label(role)} — {_now_local_heading()}\n\n{(body or '').strip()}\n"


def _append_turn_to_file(
    path: Path,
    *,
    role: str,
    subject: str,
    body: str,
    thread_id: str,
) -> None:
    """Read ``path``, append a new turn section, refresh ``updated_at``.

    If the file doesn't exist, creates it with full frontmatter and the
    first turn. Atomic write throughout.
    """
    now = _now_iso_utc()
    if path.exists():
        raw = path.read_text(encoding="utf-8")
        meta, body_text = _parse_frontmatter(raw)
        meta["updated_at"] = now
        if thread_id and not meta.get("thread_id"):
            meta["thread_id"] = thread_id
        if subject and not meta.get("subject"):
            meta["subject"] = subject
        new_section = _format_turn_section(role, body)
        new_body = body_text.rstrip() + "\n\n" + new_section
        content = _serialize_frontmatter(meta) + "\n" + new_body.lstrip("\n")
    else:
        meta = {
            "thread_id": thread_id or "",
            "subject": subject or "",
            "participants": ["david-wurtz", "deja-cos"],
            "channel": "email",
            "started_at": now,
            "updated_at": now,
        }
        title = subject.strip() or "Conversation"
        body_text = f"# {title}\n\n" + _format_turn_section(role, body)
        content = _serialize_frontmatter(meta) + "\n" + body_text
    _atomic_write(path, content)
    # Touch mtime explicitly — the wiki_catalog recency sort keys off it
    # and a tempfile+rename keeps the FS mtime of the new inode, which
    # is already "now", but make it explicit so the intent is clear.
    try:
        os.utime(path, None)
    except OSError:
        pass


def log_dialogue_turn(
    *,
    role: str,
    subject: str,
    body: str,
    thread_id: str = "",
    in_reply_to: str = "",
    message_id: str = "",
) -> None:
    """Append one user↔cos exchange to its per-thread conversation file.

    Both sides write here: the email observer logs user replies, and
    ``_send_email_to_self`` logs cos's outbound responses. Each thread
    lives in its own Markdown file under
    ``~/Deja/conversations/<date>/<slug>.md``; subsequent turns on the
    same thread append to it. QMD then indexes the files so future cos
    invocations can retrieve by topic via ``search_deja`` — the single
    append-only JSONL log couldn't support that.
    """
    try:
        _maybe_auto_migrate()
        CONVERSATIONS_DIR.mkdir(parents=True, exist_ok=True)
        slug = _conversation_slug(subject, thread_id)
        existing = _find_existing_conversation(slug)
        if existing is not None:
            path = existing
        else:
            date_dir = CONVERSATIONS_DIR / datetime.now().strftime("%Y-%m-%d")
            path = date_dir / f"{slug}.md"
        _append_turn_to_file(
            path,
            role=role,
            subject=subject,
            body=body or "",
            thread_id=thread_id,
        )
    except Exception:
        log.debug("dialogue log write failed", exc_info=True)


def conversation_slug_for(subject: str, thread_id: str) -> tuple[str, str] | None:
    """Return ``(date, slug)`` for an existing conversation file, else None.

    Used by ``_build_user_reply_payload`` to hand cos a pointer to the
    exact conversation file so it can ``get_page("conversations",
    "<date>/<slug>")`` for full history.
    """
    slug = _conversation_slug(subject, thread_id)
    path = _find_existing_conversation(slug)
    if path is None:
        return None
    date = path.parent.name
    return date, slug


# ---------------------------------------------------------------------------
# One-shot migration from the legacy ``conversations.jsonl``.
# ---------------------------------------------------------------------------


def _iter_legacy_turns() -> list[dict]:
    if not COS_DIALOGUE.exists():
        return []
    try:
        raw = COS_DIALOGUE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    turns: list[dict] = []
    for line in raw:
        line = line.strip()
        if not line:
            continue
        try:
            turns.append(json.loads(line))
        except Exception:
            continue
    return turns


def _parse_iso(ts: str) -> datetime:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return datetime.now(timezone.utc)


def migrate_dialogue_log() -> int:
    """Migrate ``conversations.jsonl`` into per-thread Markdown files.

    Groups legacy turns by ``thread_id`` (empty → each turn stands
    alone), writes the new layout under ``~/Deja/conversations/``, and
    renames the old log to ``conversations.jsonl.migrated`` so the user
    can still inspect it. Returns the number of conversation files
    written. Safe to call repeatedly — no-op when the log is missing,
    empty, or already migrated.
    """
    turns = _iter_legacy_turns()
    if not turns:
        return 0

    groups: dict[str, list[dict]] = {}
    for t in turns:
        key = t.get("thread_id") or f"__solo__:{t.get('message_id') or t.get('ts', '')}"
        groups.setdefault(key, []).append(t)

    CONVERSATIONS_DIR.mkdir(parents=True, exist_ok=True)
    written = 0
    for _, group in groups.items():
        group.sort(key=lambda t: _parse_iso(t.get("ts", "")))
        first = group[0]
        last = group[-1]
        subject = (first.get("subject") or "").strip()
        thread_id = (first.get("thread_id") or "").strip()
        slug = _conversation_slug(subject, thread_id)
        first_dt = _parse_iso(first.get("ts", "")).astimezone()
        date_dir = CONVERSATIONS_DIR / first_dt.strftime("%Y-%m-%d")
        path = date_dir / f"{slug}.md"

        participants: list[str] = []
        for t in group:
            r = _role_label(t.get("role", ""))
            who = "david-wurtz" if r == "user" else "deja-cos"
            if who not in participants:
                participants.append(who)

        meta = {
            "thread_id": thread_id,
            "subject": subject,
            "participants": participants or ["david-wurtz", "deja-cos"],
            "channel": "email",
            "started_at": _parse_iso(first.get("ts", ""))
                .astimezone(timezone.utc)
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z"),
            "updated_at": _parse_iso(last.get("ts", ""))
                .astimezone(timezone.utc)
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z"),
        }

        sections: list[str] = [f"# {subject or 'Conversation'}"]
        for t in group:
            local_ts = _parse_iso(t.get("ts", "")).astimezone().strftime(
                "%Y-%m-%d %H:%M"
            )
            body = (t.get("body") or "").strip()
            sections.append(
                f"## {_role_label(t.get('role', ''))} — {local_ts}\n\n{body}"
            )

        content = (
            _serialize_frontmatter(meta)
            + "\n"
            + "\n\n".join(sections)
            + "\n"
        )
        _atomic_write(path, content)
        written += 1

    try:
        COS_DIALOGUE.replace(
            COS_DIALOGUE.with_suffix(COS_DIALOGUE.suffix + ".migrated")
        )
    except OSError:
        log.debug("migration: failed to rename legacy jsonl", exc_info=True)

    log.info("cos: migrated %d legacy conversations into %s",
             written, CONVERSATIONS_DIR)
    return written


def _maybe_auto_migrate() -> None:
    """Run the migration lazily if the legacy log has content and the new
    directory is empty. Called from ``log_dialogue_turn`` so a fresh
    install that upgrades mid-thread doesn't lose prior turns.
    """
    try:
        if not COS_DIALOGUE.exists():
            return
        # Empty legacy file → skip.
        if COS_DIALOGUE.stat().st_size == 0:
            return
        # New dir already has content → skip.
        if CONVERSATIONS_DIR.exists() and any(CONVERSATIONS_DIR.iterdir()):
            return
    except OSError:
        return
    try:
        migrate_dialogue_log()
    except Exception:
        log.debug("auto-migration failed", exc_info=True)


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
   Goals.md is your scratchpad, fully editable by you. The whole
   file — Standing Context, Automations, Tasks, Waiting For,
   Reminders, Archive — is yours to write, edit, rearrange. The
   user edits it too in Obsidian; expect back-and-forth. When you
   notice something worth caring about that isn't urgent-now, ADD
   IT to goals.md — don't email.

   - `add_reminder({date, question, topics})` — record a concern
     with an honest best-guess date of WHEN raising it would be
     useful. Not "tomorrow" reflexively — think about when the user
     would actually want this surfaced.
   - `add_task` — an open action item.
   - `add_waiting_for` — something a third party owes.
   - `complete_task` / `resolve_waiting_for` / `resolve_reminder` /
     `archive_*` — close loops aggressively when evidence supports.
   - `update_wiki("goals", ...)` — use this to edit anything
     outside the bullet-list sections: **Standing Context**
     (durable rules like "user drives Mon/Wed carpool"), **Automations**
     (trigger→action rules), the **Archive** hygiene. Cite
     evidence in the `reason` field: the signals you saw, the
     pattern count, why the rule is worth codifying.
   - `update_wiki` — on any wiki page; only with a concrete signal
     grounding the change.
   - `browser_ask({prompt, timeout_sec?})` — reach services Deja
     doesn't have a direct API for: Google Photos, Slack, Spotify
     (podcasts), TeamSnap, LinkedIn, anywhere the user is already
     logged in via Chrome. Drives Claude-in-Chrome non-interactively.
     **Cost: ~60-120s + a brief "Claude started debugging this
     browser" infobar flash; flat-fee on Pro/Max.** So reserve it:
       - Use it for things local tools cannot answer — semantic
         photo recall, recent Spotify plays, Slack DM state,
         TeamSnap schedules, checking an auth-walled page.
       - **Do NOT use it** when `gmail_search`, `calendar_list_events`,
         `search_deja`, or `recent_activity` can answer — those are
         1-2s, authoritative, and don't poke the browser.
     Always phrase the prompt as a full task, explicitly naming the
     extension: `"Use the Chrome extension (NOT any CLI) to
     navigate to open.spotify.com, open Recently Played, and return
     the 3 most recent podcast episodes with show, episode title,
     date, and any visible description text."` End with "close the
     tabs you opened when done" if you don't want residue in the
     MCP tab group. If it returns a login-wall message, don't
     retry — surface the need-to-log-in to the user.

   **Look for automations worth codifying.** If you catch yourself
   (or a past cycle) doing the same manual reasoning across multiple
   weeks — "every Sunday I pull a kid's sports schedule and propose
   drivers," "every time a scheduling-service email arrives I create
   a calendar event," "whenever the user travels, the co-parent
   needs a handoff" — WRITE IT as an Automation in goals.md. Lead
   with the trigger, then the action, then a short rationale.
   Example:

     > **Sunday weekly plan**: On the Sunday-morning reflective
     > pass, call `browser_ask` for the upcoming week's sports
     > schedule, cross-reference with primary calendar and standing
     > driver rules, and email a scannable bullet list covering
     > per-day events + proposed driver + flagged conflicts.
     > *(Pattern: user asked for this explicitly; good candidate
     > for recurring.)*

   Don't let perfect be the enemy: a half-right automation the
   user edits in Obsidian is better than none. Propose bold. Cite
   the evidence so the user can judge.
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

The user is a builder. Terse. Specific. Actionable. State facts,
not offers. Never pad.

**Hard rules for email body:**

- **Flat bullet list — no prose paragraphs.** Every item is ONE
  bullet. Multiple items are fine as long as each is a single
  scannable line. No intro paragraph, no outro, no subsection
  headers ("Also closed two items silently:"). The whole body is
  a list.

- **Each bullet is one of three kinds**: something the user needs
  to KNOW, something they need to DO, or something YOU DID for
  them. Mix freely — don't segregate "also silently" into a
  separate block. "Resolved: Jane Mon carpool — you're home Mon"
  and "Joe 4/24 visit — lock time today, brief the household
  before Tue" sit next to each other in the same list.

- **Lead each bullet with the noun** — the person or thing —
  then the action or status. `Joe 4/24 visit — lock time and
  brief the household before Tue AM`. NOT "I noticed that Joe
  has a visit coming up and you should…"

- **No permission-seeking.** If drafting is right, draft
  (`draft_email` → Gmail drafts) and say it's drafted. Never ask
  the user for instructions in the email.

- **No meta-commentary.** "cheapest moment," "thought you'd want
  to know," "just flagging," "I noticed" — banned. Lead with
  fact or action taken, nothing else.

- **Future-date items with 2+ days of headroom** go to goals.md
  via `add_reminder`, NOT the email. If it can wait, it should
  wait there.

Good subject: `Jane replied — roof needs re-lay, quote in ~1wk`
Good body:
> - Jane (07:53) — flashing fine; deck area needs new
>   underlayment + re-lay. Quote next week.
> - Drafted ack-and-confirm reply in your Gmail drafts.

Good body (multi-item):
> - Joe 4/24-25 window-leak visit — you leave Tue AM; lock time
>   + brief the household on access today or Mon. Reminder moved
>   to Mon AM.
> - Kid Mon carpool swap — resolved (you're home Mon).
> - Kid Tue 4pm activity — resolved (co-parent has a sitter covering).

Bad: prose paragraphs, "Also closed two items silently:" sub-list,
"Old reminder fired 4/23 — too late" meta-commentary, "Want me to
draft a text? Reply and I'll send" permission-seeking.

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
demanding your attention — the point is to *think* AND to do the
periodic wiki maintenance work nobody else owns.

You now own the whole reflect pass. The deterministic sweeps that
used to auto-merge and auto-close have been retired — you're the
decision layer. Four candidate-generator MCP tools prepare the work
for you:

  - ``find_dedup_candidates`` — people/project pairs that might be
    the same entity (vector similarity ≥0.82 by default).
  - ``find_orphan_event_clusters`` — events voting for a not-yet-
    existing project (dangling slug) or vector-similar events with a
    shared non-user person.
  - ``find_open_loops_with_evidence`` — open Tasks / Waiting-fors
    paired with recent events whose keywords suggest closure.
  - ``find_contradictions`` — page pairs in the contradiction
    similarity window (0.65..0.82) that MIGHT contradict each other.

These are pure candidate lists. You decide what (if anything) to act
on.

**Procedure:**

1. **Load state.** Call ``daily_briefing`` once. Read active
   projects, open waiting-fors, reminders due in the horizon window,
   the calendar block.

2. **Run the candidate generators relevant to the slot/horizon.**
   Not every pass calls every tool — pick what fits:
     - ``horizon: "day"`` — ``find_open_loops_with_evidence`` first
       (closing stale commitments is the highest-leverage work);
       then a quick ``find_dedup_candidates`` if the wiki grew a lot
       this cycle.
     - ``horizon: "week"`` / ``"month"`` — add
       ``find_orphan_event_clusters`` and ``find_contradictions``;
       these find longer-lived drift that isn't urgent.

3. **Decide per candidate.** For each interesting candidate:
     - Read the full body via ``get_page`` so you're reasoning on
       actual content, not a snippet.
     - Verify with ``gmail_search`` / ``search_deja`` /
       ``recent_activity`` if the evidence is thin or the claims feel
       stale.
     - Then act (or silently skip):
         * **Dedup candidate** — if genuinely same entity, pick a
           canonical, write the merged body to it via
           ``update_wiki(action="write", ...)``, then delete the
           duplicate via ``update_wiki(action="delete", ...)``. If
           you're not sure, skip — re-examining next cycle is fine.
         * **Orphan cluster** — if it's a real ongoing project,
           create ``projects/<slug>.md`` via ``update_wiki`` with a
           seed paragraph plus a ``## Recent`` list linking the
           member events. Honor the ``suggested_slug`` when one is
           provided (it's the slug the events are already voting
           for). Skip coincidental clusters.
         * **Open loop with evidence** — if the event genuinely
           closes the commitment (directly OR indirectly — a
           delegated reach-out that happened, a payment that
           arrived via the promised channel), call
           ``complete_task`` or ``resolve_waiting_for`` with a
           reason citing the event slug. If evidence is ambiguous,
           skip — don't close on suspicion.
         * **Contradiction cluster** — see the escalation pattern
           below.

4. **Contradictions — escalation pattern.** Contradictions are rare
   but high-stakes. Three paths:
     - *Resolvable via tools.* One claim is clearly stale (older
       date), already corrected elsewhere, or refuted by a
       `gmail_search` result or calendar lookup → silently fix it
       via ``update_wiki`` on the stale page. Cite the current
       source in the ``reason``.
     - *Not resolvable but not blocking.* Both claims are plausible,
       neither is older, and no open loop depends on knowing which
       is true → add a one-line note in goals.md under ``## Tasks``
       via ``update_wiki("goals", ...)`` with BOTH claims and what
       you checked. Let the next cycle re-examine once more context
       arrives.
     - *Blocking an open loop or critical fact.* The user needs to
       pick → ``execute_action("send_email_to_self", {subject, body})``
       with just the question + two claims, no padding. Single-
       sentence body, two bullets, one call-to-action.

5. **Proactive chief-of-staff thinking.** After candidates, ask the
   standing question: *"what would I proactively do for the user
   right now that they haven't asked for?"* Same VERIFY BEFORE
   NOTIFYING rules apply. If you find something concrete — a draft
   email for a pre-kickoff project, a calendar reminder with
   context, a flagged reminder — NOTIFY or ACT. If not, SILENT is
   the right answer.

6. **Proposed rules.** If a pattern is worth codifying as standing
   guidance (e.g., "when the user travels for work, draft the
   co-parent a Day-1 handoff"), DO NOT silently write it to
   ``goals.md``. Include it as a proposal in your email; the user
   accepts by adding it to their Standing Context themselves.

**Tone in reflective emails.** Lead with "I was thinking ahead about
X…" or "Heads-up for the week…". Not reactive. Specific. Bundle
related items — if you find 3 things in one pass, send ONE email
with 3 bullets, not 3 emails.

**Volume.** Steady-state expectation: 6-15 decisions per reflective
pass. Most decisions are "skip — coincidental / unverified / not
blocking." A few are concrete actions. Don't manufacture work to
fill the budget; don't skip work because the budget feels tight.
The right number is the number of grounded decisions the candidate
generators actually surfaced.
"""


USER_REPLY_APPENDIX = """\

## USER REPLY MODE

When the payload has ``mode: "user_reply"`` the user replied directly
to one of your prior ``[Deja]`` emails. This is a conversation, not a
background cycle — they're TALKING TO YOU.

The payload carries:
  - ``subject``: the reply subject (e.g. "Re: [Deja] weekly carpool")
  - ``user_message``: what the user wrote, quoted history stripped
  - ``thread_id``: Gmail thread id for in-thread replies
  - ``in_reply_to``: RFC822 Message-Id for threading headers
  - ``conversation_slug``: ``"<YYYY-MM-DD>/<slug>"`` of the conversation
    file that contains the full user↔cos history for this thread (may be
    empty on the very first turn)

Your procedure:

1. **Read the message as a first-class request.** The user may be:
     a. Giving you an instruction ("actually, Jane handles this")
     b. Correcting a fact ("no, Joe is the coach, not the driver")
     c. Teaching a standing preference ("when I travel, always notify
        Jane the day before")
     d. Asking a question ("what did Joe say about the quote?")
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

4. **Carry context.** The payload's ``conversation_slug`` points at a
   Markdown file at ``~/Deja/conversations/<conversation_slug>.md``
   containing the full user↔cos history on this thread. Read it with
   ``get_page("conversations", "<conversation_slug>")`` before replying
   so you don't re-ask what was already answered two turns ago. For
   cross-thread context on a topic or person (e.g. "have I talked
   with Joe about this topic before?"), call
   ``search_deja("<topic or person>")`` — it searches conversations
   alongside wiki pages and events, so related past threads surface.

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
    elif mode == "command":
        system_prompt_text = (
            system_prompt_text.rstrip() + "\n\n" + COMMAND_APPENDIX
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
    slug_ref = conversation_slug_for(subject, thread_id)
    conversation_slug = f"{slug_ref[0]}/{slug_ref[1]}" if slug_ref else ""
    return {
        "mode": "user_reply",
        "subject": subject,
        "user_message": user_message,
        "thread_id": thread_id,
        "in_reply_to": in_reply_to,
        "message_id": message_id,
        "conversation_slug": conversation_slug,
        "ts": datetime.now(timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
    }


COMMAND_APPENDIX = """\

## COMMAND MODE

When the payload has ``mode: "command"`` the user just spoke or
typed something directly at you — voice push-to-talk (Option hold)
or the notch-panel text input. This is a live request, not an
email; they're waiting for your response RIGHT NOW in the pill.

The payload carries:
  - ``user_message``: what the user said (voice) or typed (chat),
    polished for grammar.
  - ``source``: ``"voice"`` or ``"text"``.
  - ``conversation_slug``: the per-session conversation file this
    turn was logged into.
  - ``recent_screens``: a freshness-gated snapshot of what's on
    each display RIGHT NOW — ``{display_id: {app, window_title,
    ocr, age_sec}}``. Unlike other cos modes (email/iMessage/
    WhatsApp) where the user is typically on their phone, notch
    input means the user is AT the screen. When they say "that
    thing," "this email," "what I'm looking at," "the person on
    the left" — resolve the referent from this block before
    fetching anything. Empty dict means no display had a fresh
    capture (≤5 min), so don't assume screen context in that case.

Your job is to route AND respond. Choose the right action based on
the content:

1. **Unambiguous action request** ("put dentist on my calendar
   tomorrow 3pm", "remind me to reply to Joe", "draft a reply to
   Jane"): call the appropriate MCP write tool — ``execute_action``,
   ``add_reminder``, ``add_task``, ``add_waiting_for``,
   ``update_wiki``, etc. — then confirm in your final message
   ("Added: Dentist, Fri 3pm").

2. **Instruction / correction / preference** ("actually Jane
   handles school logistics", "remember that the coach needs 3
   kids"): update the wiki via ``update_wiki`` so the fact becomes
   durable context, then confirm ("Noted on <page> — new fact
   added").

3. **Context note** ("the kid said her foot still hurts"): promote
   to the right entity page via ``update_wiki`` if it's a durable
   fact, or just acknowledge ("Noted.") if it's ephemeral.

4. **Question** ("what did Jane say about the quote?"): look it up
   via ``search_deja`` / ``get_page`` / ``gmail_search`` / other
   read tools, then answer in your final message — concise, direct.

5. **Ambiguous** — ask for clarification in your final message.
   Don't guess.

**Your final message is what shows in the pill.** It's the only
thing the user will see unless they open the conversations file.
Keep it SHORT (1-3 lines max, phone-readable), lead with the
result. No preamble, no "I'll help you with that," no asking if
they want more.

Good: "Added: Dentist, Fri 3-3:30pm."
Good: "Noted on <page> — new fact added."
Good: "Jane (Apr 16): quote coming next week; status looks fine."
Bad: "I've gone ahead and added that event to your calendar. Let
me know if there's anything else!"

The user's utterance is already logged to the conversation file
``get_page("conversations", "<conversation_slug>")`` — no need to
write it yourself. You CAN call ``update_wiki`` on the conversation
file to append YOUR response as a section, but the pill-display is
the main channel for voice/text.

**Verification still matters.** If the user gave you a fact that
contradicts the wiki, re-read the wiki page first. If the user
asked about a date/time, check the calendar. Cost of a tool call:
~2s. Cost of a wrong confirmation: they stop trusting the pill.
"""


BAGEL_APPENDIX = """\

## WHATSAPP CHANNEL MODE (Bagel)

You are operating as Bagel, the user's chief of staff reachable on
WhatsApp. This is the SAME identity as cos on voice, notch chat, and
email — same memory, same voice, same disposition. Only the channel
differs: messages arrive via WhatsApp, replies go back through
WhatsApp. Your Claude-Agent-SDK session's final message is what gets
sent to the user in-chat.

Treat this like a live text thread:

1. **Plain text only.** No Markdown fences, no bold asterisks, no
   tables, no headers. WhatsApp renders those as literal characters
   and it looks broken. Use line breaks and simple dashes if you
   need structure. Emoji is fine (and often warm).

2. **Tight replies.** One short paragraph is usually right. Multi-
   line only when the user asked for a list or status. Lead with the
   answer, not the preamble.

3. **Ignore the trigger prefix.** The user's message will start with
   ``@Bagel`` or similar — strip it before parsing intent.

4. **Deja's wiki + state are mounted at
   ``/workspace/extra/home/Deja`` and ``/workspace/extra/home/.deja``
   — read them directly with your Read / Bash / Glob / Grep tools.**
   The full Deja MCP tool surface (``search_deja``, ``get_page``,
   ``calendar_list_events``, ``gmail_search``, ``update_wiki``,
   ``add_reminder``, ``execute_action``, etc.) is NOT available in
   this channel right now — the host Python environment isn't
   runnable inside the Linux container. Workarounds:

     - Read ``/workspace/extra/home/Deja/goals.md`` for tasks +
       waiting-fors + reminders + standing context.
     - Read ``/workspace/extra/home/Deja/index.md`` for the full
       catalog of people + projects, mtime-sorted.
     - Read individual pages via
       ``/workspace/extra/home/Deja/people/<slug>.md`` and
       ``projects/<slug>.md`` and ``events/YYYY-MM-DD/<slug>.md``.
     - Read ``/workspace/extra/home/.deja/observations.jsonl`` for
       recent signals (grep / tail as needed).
     - Read
       ``/workspace/extra/home/Deja/conversations/<date>/<slug>.md``
       for prior user↔cos exchanges across channels.
     - For reminders/tasks, edit
       ``/workspace/extra/home/Deja/goals.md`` with the Edit tool
       (append to the right section, keep the existing formatting).
     - You CANNOT hit Google Calendar, Gmail, or Tasks from here —
       those require the MCP tools that aren't available. If the
       user asks for a calendar event creation, say so and offer to
       add it as a reminder in goals.md instead.

5. **Cross-channel memory.** Every direct user↔cos exchange —
   WhatsApp, email reply, voice, notch chat — lands in
   ``~/Deja/conversations/YYYY-MM-DD/<slug>.md`` on the mounted
   host filesystem. ``search_deja`` and ``get_page`` surface them.
   If the user refers to something they told you earlier — "the
   thing I mentioned this morning" — search first, don't guess.

6. **Do NOT use ``send_email_to_self``** on this channel. You're
   already talking to the user in WhatsApp; an email would be an
   unwanted duplicate push. Your reply goes via stdout, that's it.

7. **Actions apply to the real world.** ``calendar_create`` hits
   the user's Google Calendar (same as every other cos channel).
   ``add_reminder`` writes to the same goals.md. ``update_wiki``
   commits to the same ~/Deja git repo. You're not in a sandbox —
   this is production. VERIFY BEFORE WRITING.

8. **Verification matters especially here.** The user may be out of
   the house, in a meeting, distracted. A wrong reply is more
   expensive on WhatsApp than in the pill because they won't
   double-check. If a fact is wrong in the wiki, say so. If the
   calendar contradicts what they said, tell them.

**Identity note.** When introducing yourself or answering "who are
you?" type questions, you can say "I'm Bagel — your chief of staff
on WhatsApp. Same memory as Deja's notch cos." That's honest:
you're the WhatsApp face of a system that also speaks to you via
voice, email, and the notch.
"""


def _bagel_system_prompt() -> str:
    """Full system prompt for Bagel = cos DEFAULT_SYSTEM_PROMPT + the
    WhatsApp appendix. Called by ``sync_bagel_prompt`` to regenerate
    ``nanoclaw/groups/whatsapp_main/CLAUDE.md`` whenever the cos prompt
    changes.
    """
    return DEFAULT_SYSTEM_PROMPT.rstrip() + "\n\n" + BAGEL_APPENDIX


def sync_bagel_prompt(
    nanoclaw_groups_dir: Path,
    *,
    group: str = "whatsapp_main",
) -> tuple[Path, Path]:
    """Regenerate the Bagel group's CLAUDE.md + .mcp.json so the
    WhatsApp surface stays aligned with the canonical cos system
    prompt.

    ``nanoclaw_groups_dir`` points at ``~/projects/nanoclaw/groups/``
    (where each group is a subdirectory). We atomically write:

      - ``<dir>/<group>/CLAUDE.md`` — cos prompt + BAGEL_APPENDIX
      - ``<dir>/<group>/.mcp.json`` — Deja MCP server config, using
        the in-container path ``/workspace/extra/home/...`` since
        NanoClaw mounts ``/Users/wurtz`` to ``/workspace/extra/home``
        for this group.

    Returns the two written paths so callers can log / verify.

    The MCP config points at the dev venv's Python. If the user's
    deja venv lives somewhere else (e.g., a bundled app Python), they
    can hand-edit .mcp.json after sync. Sync overwrites it next time,
    so keep customizations small.
    """
    group_dir = nanoclaw_groups_dir / group
    if not group_dir.exists():
        raise FileNotFoundError(
            f"NanoClaw group dir not found: {group_dir}. Is the group "
            f"registered? Run `nanoclaw` group setup first.",
        )

    claude_md = group_dir / "CLAUDE.md"
    mcp_json = group_dir / ".mcp.json"

    tmp_md = claude_md.with_suffix(".md.tmp")
    tmp_md.write_text(_bagel_system_prompt(), encoding="utf-8")
    tmp_md.replace(claude_md)

    # Deja MCP intentionally NOT configured here — the host's .venv
    # Python is a Mac ARM64 binary (symlink to /opt/homebrew/...) and
    # can't exec inside the Linux container. Until we bake a Linux
    # Python + `pip install -e deja` into the agent image (or add SSE
    # MCP transport to Deja so the container can connect to a host
    # server), Bagel gets filesystem access to ~/Deja via the mount
    # and reads it with Read / Bash. See BAGEL_APPENDIX for workarounds.
    mcp_config = {"mcpServers": {}}
    tmp_json = mcp_json.with_suffix(".json.tmp")
    tmp_json.write_text(
        json.dumps(mcp_config, indent=2) + "\n", encoding="utf-8",
    )
    tmp_json.replace(mcp_json)

    return claude_md, mcp_json


def _recent_screens_snapshot(max_age_sec: int = 300, ocr_char_cap: int = 4000) -> dict:
    """Return freshest OCR+AX per display for command-mode preload.

    The notch chat/voice input is the one channel where the user is
    verifiably AT the screen at the moment of speaking — often
    referencing something visible ("that thing"). Preloading the
    latest OCR text + AX frontmost-window metadata lets cos ground
    pronouns without calling `recent_activity` first.

    Only returns displays whose latest raw_ocr file is fresh
    (``<= max_age_sec`` old); stale screens are omitted entirely
    rather than silently fed as current.
    """
    from deja.config import DEJA_HOME

    out: dict[str, dict] = {}
    try:
        day = datetime.now().strftime("%Y-%m-%d")
        ocr_dir = DEJA_HOME / "raw_ocr" / day
        if not ocr_dir.exists():
            return out
        now = datetime.now().timestamp()
        # Group files by display prefix; pick the newest per prefix.
        latest_by_display: dict[str, Path] = {}
        for f in ocr_dir.iterdir():
            if not f.is_file() or not f.name.endswith(".txt"):
                continue
            prefix = f.name.split("-", 1)[0]
            if not prefix.startswith("display"):
                continue
            display_id = "-".join(f.name.split("-")[:2])  # "display-1"
            existing = latest_by_display.get(display_id)
            if existing is None or f.stat().st_mtime > existing.stat().st_mtime:
                latest_by_display[display_id] = f
        for display_id, path in latest_by_display.items():
            age = now - path.stat().st_mtime
            if age > max_age_sec:
                continue
            try:
                ocr = path.read_text(encoding="utf-8")[:ocr_char_cap]
            except OSError:
                continue
            ax_path = DEJA_HOME / f"screen_{display_id.split('-')[1]}_ax.json"
            ax: dict = {}
            if ax_path.exists():
                try:
                    ax = json.loads(ax_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    ax = {}
            out[display_id] = {
                "age_sec": round(age, 1),
                "app": ax.get("app", ""),
                "window_title": ax.get("window_title", ""),
                "ocr": ocr,
            }
    except Exception:
        log.debug("recent_screens snapshot failed", exc_info=True)
    return out


def _build_command_payload(
    *,
    user_message: str,
    source: str,
    conversation_slug: str,
) -> dict[str, Any]:
    """Payload for a voice-or-text command routed directly to cos."""
    return {
        "mode": "command",
        "user_message": user_message,
        "source": source,
        "conversation_slug": conversation_slug,
        "recent_screens": _recent_screens_snapshot(),
        "ts": datetime.now(timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
    }


def invoke_command_sync(
    *,
    user_message: str,
    source: str = "voice",
) -> tuple[int, str, str]:
    """Fire cos in command mode and block until done.

    Called from ``/api/mic/stop`` and ``/api/command`` when a voice
    utterance or chat message needs routing. Logs the user's turn to
    the conversations/ store, then spawns cos. Returns
    ``(rc, cos_reply_text, stderr)`` where ``cos_reply_text`` is the
    claude subprocess's stdout (used for pill display).

    The caller is responsible for bubbling ``cos_reply_text`` into the
    HTTP response so the notch pill can render it.
    """
    if not is_enabled():
        return (0, "(cos disabled)", "")
    if not COS_SYSTEM_PROMPT.exists() or not COS_MCP_CONFIG.exists():
        _ensure_cos_dir()

    subject = f"{source.capitalize()} — {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    thread_id = f"{source}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    log_dialogue_turn(
        role="user",
        subject=subject,
        body=user_message,
        thread_id=thread_id,
    )
    ds = conversation_slug_for(subject, thread_id)
    conversation_slug = f"{ds[0]}/{ds[1]}" if ds else ""

    payload = _build_command_payload(
        user_message=user_message,
        source=source,
        conversation_slug=conversation_slug,
    )
    rc, stdout, stderr = _run_claude(payload)
    _log_invocation(
        cycle_id=f"command/{thread_id}",
        payload=payload,
        rc=rc,
        stdout=stdout,
        stderr=stderr,
    )
    # Append cos's reply to the conversation file so future cos cycles
    # see it in context. Also trim final claude stdout to the last
    # substantive line — claude -p output format "text" puts the final
    # response on the last non-empty line.
    reply_text = (stdout or "").strip()
    if reply_text:
        try:
            log_dialogue_turn(
                role="cos",
                subject=subject,
                body=reply_text,
                thread_id=thread_id,
            )
        except Exception:
            log.debug("command: cos reply log failed", exc_info=True)
    return rc, reply_text, stderr


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
    this, so the conversation file the payload's ``conversation_slug``
    points at will already contain it.
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
    "CONVERSATIONS_DIR",
    "DEFAULT_SYSTEM_PROMPT",
    "REFLECTIVE_APPENDIX",
    "USER_REPLY_APPENDIX",
    "COMMAND_APPENDIX",
    "BAGEL_APPENDIX",
    "sync_bagel_prompt",
    "is_enabled",
    "enable",
    "disable",
    "invoke",
    "invoke_sync",
    "invoke_reflective_sync",
    "invoke_user_reply",
    "invoke_user_reply_sync",
    "invoke_command_sync",
    "log_dialogue_turn",
    "migrate_dialogue_log",
    "conversation_slug_for",
]
