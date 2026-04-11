"""Right-now briefing — what the user should be considering.

Reads goals.md + observations.jsonl and returns a structured dict
covering the four answerable-without-LLM questions:

  1. What reminders are due today?
  2. What tasks are overdue or due soon (deadline parsed from task text)?
  3. What waiting-fors are stale (added > 7 days ago but not yet at the
     21-day auto-archive line — the "should probably ping" zone)?
  4. How many open items total, by section?

Pure Python date math. No LLM. Expected runtime: a few milliseconds.
Consumed by ``GET /api/briefing`` and rendered by the Swift notch
briefing panel. The user's stated goal: stop needing to open goals.md
in Obsidian to know what's in front of them.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from typing import Any

from deja.goals import (
    GOALS_PATH,
    _parse_added_date,
    _parse_reminder_due,
    _parse_sections,
)

log = logging.getLogger(__name__)


STALE_WAITING_MIN_DAYS = 7
UPCOMING_TASK_WINDOW_DAYS = 3

# Deadline extraction from free-text task lines. Covers the formats the
# integrate prompt is told to emit and the ones users naturally type:
#
#   "— by April 12"
#   "— by Apr 12"
#   "— by Friday April 12"
#   "— deadline: April 12"
#   "— deadline: 2026-04-12"
#   "— by 2026-04-12"
#   "(by April 12)"
#
# We match a month name or ISO date and parse with dateutil fallback
# via datetime. Month-word parsing without a year defaults to the
# closest future year so "Apr 12" in April '26 resolves to 2026-04-12.
_MONTH_NAMES = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}

_ISO_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_MONTH_DAY_RE = re.compile(
    r"\b(?:by|deadline:?|due:?)\s+(?:(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*\s+)?"
    r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
    r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
    r"\s+(\d{1,2})(?:,?\s+(\d{4}))?",
    re.IGNORECASE,
)


def _parse_task_deadline(text: str, today: date) -> date | None:
    """Best-effort deadline extraction from a task line. Returns None on miss.

    Prefers ISO date anywhere in the string. Falls back to ``by Month Day``
    style which is the most common natural phrasing. Month-word parsing
    without a year resolves to the closest future year so "Apr 12" in
    April '26 → 2026-04-12, and "Apr 12" in December '26 → 2027-04-12.
    """
    if not text:
        return None

    m_iso = _ISO_DATE_RE.search(text)
    if m_iso:
        try:
            return datetime.strptime(m_iso.group(1), "%Y-%m-%d").date()
        except ValueError:
            pass

    m_md = _MONTH_DAY_RE.search(text)
    if m_md:
        month_word = m_md.group(1).lower()
        day = int(m_md.group(2))
        year_str = m_md.group(3)
        month = _MONTH_NAMES.get(month_word)
        if month is None:
            return None
        if year_str:
            try:
                return date(int(year_str), month, day)
            except ValueError:
                return None
        # No year given: pick the closest non-past year.
        for year in (today.year, today.year + 1):
            try:
                candidate = date(year, month, day)
            except ValueError:
                continue
            if candidate >= today - timedelta(days=7):
                return candidate
    return None


def _unchecked_lines(section_lines: list[str]) -> list[str]:
    """Return only the ``- [ ] ...`` unchecked bullet lines from a section."""
    out: list[str] = []
    for line in section_lines:
        stripped = line.lstrip()
        if stripped.startswith("- [ ]"):
            out.append(line)
    return out


def _reminder_lines(section_lines: list[str]) -> list[str]:
    """Return reminder bullet lines from the Reminders section."""
    return [ln for ln in section_lines if ln.lstrip().startswith("- [")]


def _clean_bullet(line: str) -> str:
    """Strip leading ``- [ ]`` or ``- [...]`` and return the body text."""
    text = line.lstrip()
    if text.startswith("- [ ] "):
        return text[6:].strip()
    if text.startswith("- "):
        return text[2:].strip()
    return text.strip()


def _extract_reminder(line: str) -> dict[str, Any] | None:
    """Parse a reminder bullet into {date, question, topics}."""
    m = re.match(
        r"\s*-\s+\[(\d{4}-\d{2}-\d{2})\]\s+(.*)$",
        line,
    )
    if not m:
        return None
    due = m.group(1)
    rest = m.group(2).strip()
    topics: list[str] = []
    if "→" in rest:
        question, topic_part = rest.split("→", 1)
        for tm in re.finditer(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]", topic_part):
            topics.append(tm.group(1).strip())
    else:
        question = rest
    return {
        "date": due,
        "question": question.strip().rstrip(","),
        "topics": topics,
    }


def build_briefing(today: date | None = None) -> dict[str, Any]:
    """Return the structured briefing dict for ``GET /api/briefing``.

    Caller-supplied ``today`` for testability; defaults to today.
    Never raises — missing goals.md returns zero counts and empty lists.
    """
    if today is None:
        today = date.today()

    result: dict[str, Any] = {
        "counts": {
            "tasks_open": 0,
            "waiting_open": 0,
            "reminders_total": 0,
            "reminders_due": 0,
        },
        "due_reminders": [],
        "overdue_tasks": [],
        "upcoming_tasks": [],
        "stale_waiting": [],
    }

    if not GOALS_PATH.exists():
        return result

    try:
        text = GOALS_PATH.read_text(encoding="utf-8")
    except OSError:
        return result

    _, sections = _parse_sections(text)

    # --- Tasks: parse deadlines, classify as overdue / upcoming / other ---
    tasks_section = sections.get("Tasks", [])
    open_tasks = _unchecked_lines(tasks_section)
    result["counts"]["tasks_open"] = len(open_tasks)

    for line in open_tasks:
        body = _clean_bullet(line)
        deadline = _parse_task_deadline(body, today)
        if deadline is None:
            continue
        delta = (deadline - today).days
        if delta < 0:
            result["overdue_tasks"].append(
                {
                    "text": body,
                    "deadline": deadline.isoformat(),
                    "days_overdue": -delta,
                }
            )
        elif delta <= UPCOMING_TASK_WINDOW_DAYS:
            result["upcoming_tasks"].append(
                {
                    "text": body,
                    "deadline": deadline.isoformat(),
                    "days_until": delta,
                }
            )

    result["overdue_tasks"].sort(key=lambda d: d["days_overdue"], reverse=True)
    result["upcoming_tasks"].sort(key=lambda d: d["days_until"])

    # --- Waiting for: surface items in the stale zone (7..21 days) ---
    waiting_section = sections.get("Waiting for", [])
    open_waiting = _unchecked_lines(waiting_section)
    result["counts"]["waiting_open"] = len(open_waiting)

    for line in open_waiting:
        added = _parse_added_date(line)
        if added is None:
            continue
        days = (today - added).days
        if STALE_WAITING_MIN_DAYS <= days <= 21:
            result["stale_waiting"].append(
                {
                    "text": _clean_bullet(line),
                    "added": added.isoformat(),
                    "days_stale": days,
                }
            )

    result["stale_waiting"].sort(key=lambda d: d["days_stale"], reverse=True)

    # --- Reminders: due (date ≤ today) and total ---
    reminders_section = sections.get("Reminders", [])
    reminder_rows = _reminder_lines(reminders_section)
    result["counts"]["reminders_total"] = len(reminder_rows)

    for line in reminder_rows:
        parsed = _extract_reminder(line)
        if parsed is None:
            continue
        due = _parse_reminder_due(line)
        if due is None or due > today:
            continue
        result["due_reminders"].append(parsed)

    result["due_reminders"].sort(key=lambda d: d.get("date", ""))
    result["counts"]["reminders_due"] = len(result["due_reminders"])

    return result
