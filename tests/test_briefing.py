"""briefing.py — 'right now' panel data derivation tests.

The briefing endpoint runs every 10 seconds while the notch panel is
open and drives the UI the user actually sees. Regressions in deadline
parsing, stale-waiting thresholds, or section counts show up as missing
or wrong rows in that panel, so these tests lock in the invariants.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from deja import briefing


def _seed(wiki, body: str) -> None:
    (wiki / "goals.md").write_text(body)


# ---------------------------------------------------------------------------
# Deadline parsing — the load-bearing bit for "overdue" and "due soon"
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,today,expected",
    [
        # ISO dates anywhere in the string
        ("send deck — 2026-04-12", date(2026, 4, 11), date(2026, 4, 12)),
        ("by 2026-04-05", date(2026, 4, 11), date(2026, 4, 5)),
        # "by Month Day" without a year — resolves to nearest future
        ("call roofer — by April 10", date(2026, 4, 11), date(2026, 4, 10)),
        ("call roofer — by April 15", date(2026, 4, 11), date(2026, 4, 15)),
        # "deadline: Month Day"
        ("thing — deadline: May 2", date(2026, 4, 11), date(2026, 5, 2)),
        # Month abbreviations
        ("ship thing — by Apr 12", date(2026, 4, 11), date(2026, 4, 12)),
        # No deadline at all
        ("just some task", date(2026, 4, 11), None),
    ],
)
def test_parse_task_deadline(text, today, expected):
    assert briefing._parse_task_deadline(text, today) == expected


# ---------------------------------------------------------------------------
# End-to-end build_briefing
# ---------------------------------------------------------------------------


_GOALS_TEMPLATE = (
    "# Goals\n\n"
    "## Standing context\n\n\n"
    "## Automations\n\n\n"
    "## Tasks\n\n{tasks}\n"
    "## Waiting for\n\n{waitings}\n"
    "## Reminders\n\n{reminders}\n"
    "## Archive\n\n"
)


def test_empty_goals_returns_zero_counts(isolated_home):
    _, wiki = isolated_home
    _seed(wiki, _GOALS_TEMPLATE.format(tasks="", waitings="", reminders=""))

    result = briefing.build_briefing(today=date(2026, 4, 11))
    assert result["counts"]["tasks_open"] == 0
    assert result["counts"]["waiting_open"] == 0
    assert result["counts"]["reminders_total"] == 0
    assert result["due_reminders"] == []
    assert result["overdue_tasks"] == []
    assert result["upcoming_tasks"] == []
    assert result["stale_waiting"] == []


def test_overdue_task_surfaces_with_days_count(isolated_home):
    _, wiki = isolated_home
    _seed(
        wiki,
        _GOALS_TEMPLATE.format(
            tasks="- [ ] send amanda the deck — by April 5\n",
            waitings="",
            reminders="",
        ),
    )
    result = briefing.build_briefing(today=date(2026, 4, 11))

    assert len(result["overdue_tasks"]) == 1
    overdue = result["overdue_tasks"][0]
    assert "amanda" in overdue["text"].lower()
    assert overdue["days_overdue"] == 6
    assert overdue["deadline"] == "2026-04-05"


def test_upcoming_tasks_within_3_day_window(isolated_home):
    _, wiki = isolated_home
    _seed(
        wiki,
        _GOALS_TEMPLATE.format(
            tasks=(
                "- [ ] due tomorrow — by April 12\n"
                "- [ ] due in 3 days — by April 14\n"
                "- [ ] due in 10 days — by April 21\n"  # outside window
            ),
            waitings="",
            reminders="",
        ),
    )
    result = briefing.build_briefing(today=date(2026, 4, 11))

    assert len(result["upcoming_tasks"]) == 2
    days = sorted([t["days_until"] for t in result["upcoming_tasks"]])
    assert days == [1, 3]


def test_stale_waiting_7_to_21_day_window(isolated_home):
    _, wiki = isolated_home
    _seed(
        wiki,
        _GOALS_TEMPLATE.format(
            tasks="",
            waitings=(
                "- [ ] **Recent** — thing (added 2026-04-09)\n"        # 2d, too fresh
                "- [ ] **Ping me** — thing (added 2026-04-02)\n"       # 9d, in zone
                "- [ ] **Also ping** — thing (added 2026-03-28)\n"     # 14d, in zone
                "- [ ] **Out of zone** — thing (added 2026-03-15)\n"   # 27d, past window
            ),
            reminders="",
        ),
    )
    result = briefing.build_briefing(today=date(2026, 4, 11))

    names = {w["text"] for w in result["stale_waiting"]}
    assert any("Ping me" in n for n in names)
    assert any("Also ping" in n for n in names)
    assert not any("Recent" in n for n in names)
    assert not any("Out of zone" in n for n in names)


def test_due_reminders_surfaced_today_and_earlier(isolated_home):
    _, wiki = isolated_home
    _seed(
        wiki,
        _GOALS_TEMPLATE.format(
            tasks="",
            waitings="",
            reminders=(
                "- [2026-04-05] already overdue → [[amanda-peffer]]\n"
                "- [2026-04-11] due today → [[blade-and-rose]]\n"
                "- [2026-04-20] not yet → [[casita-roof]]\n"
            ),
        ),
    )
    result = briefing.build_briefing(today=date(2026, 4, 11))

    assert result["counts"]["reminders_total"] == 3
    assert result["counts"]["reminders_due"] == 2
    questions = [r["question"] for r in result["due_reminders"]]
    assert any("overdue" in q for q in questions)
    assert any("due today" in q for q in questions)
    assert not any("not yet" in q for q in questions)


def test_briefing_handles_missing_goals_md(isolated_home):
    # No goals.md at all — briefing must return zeros, not raise
    _, wiki = isolated_home
    result = briefing.build_briefing(today=date(2026, 4, 11))
    assert result["counts"]["tasks_open"] == 0
    assert result["due_reminders"] == []
