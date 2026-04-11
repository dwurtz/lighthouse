"""Goals manager — agent-maintained task list, waiting-for tracker, and
agent-scheduled reminders.

goals.md has five sections with three different ownership models:

- **User-managed** (agent reads, never writes): ``## Standing context``,
  ``## Automations``.
- **Mixed** (user can write, agent can complete-on-evidence and archive):
  ``## Tasks`` — commitments the user made to themselves. Agent can
  ``complete`` when a signal confirms it happened, and ``archive`` when
  evidence is strong (project closed, user retraction). Agent never
  deletes a task outright.
- **Agent-managed** (agent CRUDs freely): ``## Waiting for`` (things
  others owe the user), ``## Reminders`` (scheduled checks the agent
  set for its future self), and ``## Archive`` (a flat inspection list
  for anything that was silently expired).

On every write we also run hygiene:

- **Auto-expire waiting-fors** older than 21 days → moved to Archive.
- **Auto-expire reminders** more than 14 days past due → moved to Archive.
- **Cap** ``## Waiting for`` at 30 items, ``## Reminders`` at 30,
  ``## Archive`` at 100 (FIFO eviction). Tasks are uncapped on purpose
  — user intent is sacred.

Every mutation emits exactly one ``audit.record()`` entry so ``why did
this happen`` is answerable by ``jq ~/.deja/audit.jsonl``.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from pathlib import Path

from deja import audit
from deja.config import WIKI_DIR

log = logging.getLogger(__name__)

GOALS_PATH = WIKI_DIR / "goals.md"

_SECTION_ORDER = [
    "Standing context",
    "Automations",
    "Tasks",
    "Waiting for",
    "Reminders",
    "Archive",
]

WAITING_EXPIRY_DAYS = 21
REMINDER_PAST_DUE_DAYS = 14

CAP_WAITING = 30
CAP_REMINDERS = 30
CAP_ARCHIVE = 100

_ADDED_RE = re.compile(r"\(added (\d{4}-\d{2}-\d{2})\)")
_REMINDER_DATE_RE = re.compile(r"^\s*-\s+\[(\d{4}-\d{2}-\d{2})\]\s+")
_BULLET_RE = re.compile(r"^\s*-\s+")
_UNCHECKED_RE = re.compile(r"^\s*-\s+\[\s\]\s+")


# ---------------------------------------------------------------------------
# Section parser
# ---------------------------------------------------------------------------


def _parse_sections(text: str) -> tuple[list[str], dict[str, list[str]]]:
    """Split goals.md into (preamble_lines, {section_name: [lines]}).

    Preamble is everything before the first ``## `` heading (typically
    the ``# Goals`` H1 and a blank line). Each section's line list
    contains every line between its heading and the next heading,
    preserving blank lines so render is faithful.
    """
    preamble: list[str] = []
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m:
            current = m.group(1).strip()
            sections.setdefault(current, [])
            continue
        if current is None:
            preamble.append(line)
        else:
            sections[current].append(line)
    return preamble, sections


def _render_sections(
    preamble: list[str], sections: dict[str, list[str]]
) -> str:
    """Reassemble preamble + sections into goals.md text.

    Sections listed in ``_SECTION_ORDER`` come in that order; any
    unknown sections (e.g. user-added ``## Notes``) come last in the
    order the parser first encountered them.
    """
    out: list[str] = list(preamble)
    if out and out[-1] != "":
        out.append("")

    seen: set[str] = set()
    for name in _SECTION_ORDER:
        if name in sections:
            out.append(f"## {name}")
            out.extend(sections[name])
            seen.add(name)
    for name, lines in sections.items():
        if name in seen:
            continue
        out.append(f"## {name}")
        out.extend(lines)

    return "\n".join(out).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Bullet helpers
# ---------------------------------------------------------------------------


def _bullet_lines(section: list[str]) -> list[int]:
    """Return the indices of lines in ``section`` that are bullets."""
    return [i for i, ln in enumerate(section) if _BULLET_RE.match(ln)]


def _append_bullet(section: list[str], bullet_text: str) -> None:
    """Append a bullet to a section, preserving trailing blank line discipline."""
    line = f"- {bullet_text}"
    while section and section[-1].strip() == "":
        section.pop()
    section.append(line)
    section.append("")


def _ensure_section(
    sections: dict[str, list[str]], name: str
) -> list[str]:
    if name not in sections:
        sections[name] = [""]
    return sections[name]


def _substring_match(line: str, needle: str) -> bool:
    return needle.strip().lower() in line.lower()


def _today_iso() -> str:
    return date.today().isoformat()


# ---------------------------------------------------------------------------
# Auto-expiry (run on every write)
# ---------------------------------------------------------------------------


def _parse_added_date(line: str) -> date | None:
    m = _ADDED_RE.search(line)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d").date()
    except ValueError:
        return None


def _parse_reminder_due(line: str) -> date | None:
    m = _REMINDER_DATE_RE.match(line)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d").date()
    except ValueError:
        return None


def _auto_expire(
    sections: dict[str, list[str]], today: date, trigger: dict
) -> int:
    """Move stale waiting-fors and past-due reminders to Archive.

    Returns the number of items moved.
    """
    moved = 0
    archive = _ensure_section(sections, "Archive")

    # --- Waiting for: older than WAITING_EXPIRY_DAYS ---
    waiting = sections.get("Waiting for", [])
    if waiting:
        keep: list[str] = []
        for ln in waiting:
            added = _parse_added_date(ln)
            if (
                _UNCHECKED_RE.match(ln)
                and added is not None
                and (today - added).days > WAITING_EXPIRY_DAYS
            ):
                reason = f"no response in {(today - added).days} days"
                archive_line = f"{ln.rstrip()} — archived {today.isoformat()}: {reason}"
                _append_bullet(archive, archive_line.lstrip("- ").strip())
                audit.record(
                    "waiting_archive",
                    target=ln.strip(),
                    reason=reason,
                    trigger={"kind": "expiry", "detail": f"age>{WAITING_EXPIRY_DAYS}d"},
                )
                moved += 1
                continue
            keep.append(ln)
        sections["Waiting for"] = keep

    # --- Reminders: more than REMINDER_PAST_DUE_DAYS past their due date ---
    reminders = sections.get("Reminders", [])
    if reminders:
        keep2: list[str] = []
        for ln in reminders:
            due = _parse_reminder_due(ln)
            if due is not None and (today - due).days > REMINDER_PAST_DUE_DAYS:
                days_overdue = (today - due).days
                reason = f"{days_overdue} days past due"
                archive_line = f"{ln.rstrip()} — archived {today.isoformat()}: {reason}"
                _append_bullet(archive, archive_line.lstrip("- ").strip())
                audit.record(
                    "reminder_archive",
                    target=ln.strip(),
                    reason=reason,
                    trigger={"kind": "expiry", "detail": f"past_due>{REMINDER_PAST_DUE_DAYS}d"},
                )
                moved += 1
                continue
            keep2.append(ln)
        sections["Reminders"] = keep2

    return moved


# ---------------------------------------------------------------------------
# Cap enforcement
# ---------------------------------------------------------------------------


def _enforce_caps(sections: dict[str, list[str]]) -> int:
    """FIFO-evict overflow from capped sections. Tasks are uncapped."""
    evicted = 0

    for section_name, cap in (
        ("Waiting for", CAP_WAITING),
        ("Reminders", CAP_REMINDERS),
        ("Archive", CAP_ARCHIVE),
    ):
        lines = sections.get(section_name, [])
        bullet_idxs = _bullet_lines(lines)
        if len(bullet_idxs) <= cap:
            continue
        to_drop = len(bullet_idxs) - cap
        drop_set = set(bullet_idxs[:to_drop])
        kept = [ln for i, ln in enumerate(lines) if i not in drop_set]
        sections[section_name] = kept
        for i in bullet_idxs[:to_drop]:
            audit.record(
                "cap_evict",
                target=f"goals/{section_name.lower().replace(' ', '_')}",
                reason=f"cap {cap} exceeded, dropped oldest",
                trigger={"kind": "expiry", "detail": f"cap={cap}"},
            )
        evicted += to_drop
        log.info("goals: evicted %d from %s (cap %d)", to_drop, section_name, cap)

    return evicted


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


def _op_add_task(section: list[str], task: str) -> bool:
    task = task.strip()
    if not task:
        return False
    joined = "\n".join(section).lower()
    if task.lower() in joined:
        return False
    _append_bullet(section, f"[ ] {task}")
    audit.record("task_add", target="goals/tasks", reason=task[:200])
    return True


def _op_complete(section: list[str], needle: str, action: str, target: str) -> bool:
    needle = (needle or "").strip()
    if not needle:
        return False
    for i, line in enumerate(section):
        if _UNCHECKED_RE.match(line) and _substring_match(line, needle):
            section[i] = line.replace("- [ ]", "- [x]", 1)
            audit.record(action, target=target, reason=line.strip()[:200])
            return True
    return False


def _op_add_waiting(section: list[str], item: str, today: date) -> bool:
    item = item.strip()
    if not item:
        return False
    joined = "\n".join(section).lower()
    if item.lower() in joined:
        return False
    if "(added " not in item.lower():
        item = f"{item} (added {today.isoformat()})"
    _append_bullet(section, f"[ ] {item}")
    audit.record("waiting_add", target="goals/waiting_for", reason=item[:200])
    return True


def _op_add_reminder(
    section: list[str], reminder: dict, today: date
) -> bool:
    """Append a reminder bullet. ``reminder`` is ``{date, question, topics?}``."""
    if not isinstance(reminder, dict):
        return False
    raw_date = (reminder.get("date") or "").strip()
    question = (reminder.get("question") or "").strip()
    topics = reminder.get("topics") or []
    if not question or not raw_date:
        return False
    try:
        datetime.strptime(raw_date, "%Y-%m-%d")
    except ValueError:
        log.warning("goals: reminder date not YYYY-MM-DD: %s", raw_date)
        return False
    joined = "\n".join(section).lower()
    if question.lower() in joined:
        return False
    topic_str = ""
    if topics:
        topic_str = " → " + ", ".join(
            f"[[{t}]]" if not t.startswith("[[") else t for t in topics
        )
    bullet = f"[{raw_date}] {question}{topic_str}"
    _append_bullet(section, bullet)
    audit.record(
        "reminder_add",
        target="goals/reminders",
        reason=f"[{raw_date}] {question[:160]}",
    )
    return True


def _op_archive_from(
    src_section: list[str],
    archive_section: list[str],
    needle: str,
    today: date,
    reason: str,
    action: str,
    src_target: str,
) -> bool:
    needle = (needle or "").strip()
    if not needle:
        return False
    for i, line in enumerate(src_section):
        if not _BULLET_RE.match(line):
            continue
        if not _substring_match(line, needle):
            continue
        archive_line = (
            f"{line.rstrip()} — archived {today.isoformat()}: {reason or 'stale'}"
        )
        src_section.pop(i)
        _append_bullet(
            archive_section, archive_line.lstrip("- ").strip()
        )
        audit.record(action, target=src_target, reason=(reason or "stale")[:200])
        return True
    return False


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def apply_tasks_update(update: dict) -> int:
    """Apply structured changes to goals.md. Nine operations supported.

    User-intent operations (conservative):
      - add_tasks: list[str]
      - complete_tasks: list[str]
      - archive_tasks: list[{"needle": str, "reason": str}]

    External-party tracking (agent-managed):
      - add_waiting: list[str]
      - resolve_waiting: list[str]
      - archive_waiting: list[{"needle": str, "reason": str}]

    Agent self-scheduling (full CRUD):
      - add_reminders: list[{"date": "YYYY-MM-DD", "question": str, "topics": list[str]}]
      - resolve_reminders: list[str]  (substring match on question)
      - archive_reminders: list[{"needle": str, "reason": str}]

    Auto-expiry runs unconditionally on every write: waiting-fors older
    than 21 days and reminders more than 14 days past due are moved to
    Archive before the caller's operations are applied. Section caps are
    enforced at the end. Every mutation emits one ``audit.record()``.

    Returns the number of changes applied. Never touches Standing
    context or Automations.
    """
    if not GOALS_PATH.exists():
        log.warning("goals.md not found at %s", GOALS_PATH)
        return 0

    update = update or {}
    text = GOALS_PATH.read_text(encoding="utf-8")
    preamble, sections = _parse_sections(text)

    # Ensure agent-owned sections exist even if the user's goals.md is
    # from a pre-reminders install — we add the scaffold so subsequent
    # writes land in the right place.
    for sec in ("Tasks", "Waiting for", "Reminders", "Archive"):
        _ensure_section(sections, sec)

    today = date.today()
    changes = 0

    # Pass 0: auto-expiry
    changes += _auto_expire(sections, today, update)

    # Pass 1: user tasks (conservative)
    tasks = sections["Tasks"]
    for task in update.get("add_tasks") or []:
        if _op_add_task(tasks, task):
            changes += 1
    for needle in update.get("complete_tasks") or []:
        if _op_complete(tasks, needle, action="task_complete", target="goals/tasks"):
            changes += 1
    for item in update.get("archive_tasks") or []:
        if isinstance(item, str):
            item = {"needle": item, "reason": "stale"}
        if _op_archive_from(
            tasks,
            sections["Archive"],
            item.get("needle", ""),
            today,
            item.get("reason", "stale"),
            action="task_archive",
            src_target="goals/tasks",
        ):
            changes += 1

    # Pass 2: waiting-fors
    waiting = sections["Waiting for"]
    for item in update.get("add_waiting") or []:
        if _op_add_waiting(waiting, item, today):
            changes += 1
    for needle in update.get("resolve_waiting") or []:
        if _op_complete(
            waiting,
            needle,
            action="waiting_resolve",
            target="goals/waiting_for",
        ):
            changes += 1
    for item in update.get("archive_waiting") or []:
        if isinstance(item, str):
            item = {"needle": item, "reason": "stale"}
        if _op_archive_from(
            waiting,
            sections["Archive"],
            item.get("needle", ""),
            today,
            item.get("reason", "stale"),
            action="waiting_archive",
            src_target="goals/waiting_for",
        ):
            changes += 1

    # Pass 3: reminders
    reminders = sections["Reminders"]
    for rem in update.get("add_reminders") or []:
        if _op_add_reminder(reminders, rem, today):
            changes += 1
    for needle in update.get("resolve_reminders") or []:
        if _op_complete(
            reminders,
            needle,
            action="reminder_resolve",
            target="goals/reminders",
        ):
            changes += 1
    for item in update.get("archive_reminders") or []:
        if isinstance(item, str):
            item = {"needle": item, "reason": "stale"}
        if _op_archive_from(
            reminders,
            sections["Archive"],
            item.get("needle", ""),
            today,
            item.get("reason", "stale"),
            action="reminder_archive",
            src_target="goals/reminders",
        ):
            changes += 1

    # Pass 4: cap enforcement
    changes += _enforce_caps(sections)

    if changes:
        new_text = _render_sections(preamble, sections)
        GOALS_PATH.write_text(new_text, encoding="utf-8")

    return changes


# ---------------------------------------------------------------------------
# Reminder retrieval helper (used by wiki_retriever)
# ---------------------------------------------------------------------------


def due_reminder_topics(today: date | None = None) -> list[str]:
    """Return distinct ``[[slug]]`` targets from reminders due today or earlier.

    Used by ``wiki_retriever`` to augment retrieval with reminder topics
    so the integrate cycle has the relevant pages in context when it
    acts on a due reminder.
    """
    if today is None:
        today = date.today()
    if not GOALS_PATH.exists():
        return []
    try:
        text = GOALS_PATH.read_text(encoding="utf-8")
    except OSError:
        return []
    _, sections = _parse_sections(text)
    reminders = sections.get("Reminders", [])
    out: list[str] = []
    seen: set[str] = set()
    for line in reminders:
        due = _parse_reminder_due(line)
        if due is None or due > today:
            continue
        for m in re.finditer(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]", line):
            slug = m.group(1).strip()
            if slug and slug not in seen:
                out.append(slug)
                seen.add(slug)
    return out


# ---------------------------------------------------------------------------
# Automations writer (unchanged — still used by command_routes)
# ---------------------------------------------------------------------------


def append_to_automations_section(rule_text: str) -> None:
    """Append a user-authored automation rule to goals.md ## Automations.

    Raises RuntimeError if goals.md or the section is missing.
    """
    rule_text = (rule_text or "").strip()
    if not rule_text:
        raise RuntimeError("append_to_automations_section: empty rule_text")

    if not GOALS_PATH.exists():
        raise RuntimeError(
            f"goals.md not found at {GOALS_PATH}. Run setup or "
            f"investigate — every installed wiki should have one."
        )

    text = GOALS_PATH.read_text(encoding="utf-8")
    preamble, sections = _parse_sections(text)
    if "Automations" not in sections:
        raise RuntimeError(
            "goals.md is missing the '## Automations' section. Add it "
            "manually (between ## Standing context and ## Tasks) and "
            "retry — the agent will not auto-create user-managed sections."
        )

    joined = "\n".join(sections["Automations"]).lower()
    if rule_text.lower() in joined:
        log.info("automation: rule already in goals.md, skipping: %s", rule_text[:80])
        return

    _append_bullet(sections["Automations"], rule_text)
    GOALS_PATH.write_text(_render_sections(preamble, sections), encoding="utf-8")
    audit.record(
        "automation_add",
        target="goals/automations",
        reason=rule_text[:200],
        trigger={"kind": "user_cmd", "detail": "command classifier"},
    )
