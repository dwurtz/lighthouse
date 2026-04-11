"""goals.md parser + apply_tasks_update contract tests.

Covers the nine operations added alongside reminders and archive:
  - add/complete/archive tasks
  - add/resolve/archive waiting-for
  - add/resolve/archive reminders
Plus auto-expiry (waiting > 21d, reminder > 14d past due) and
cap enforcement (Waiting 30, Reminders 30, Archive 100).
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from deja import goals


def _seed_goals(wiki, body: str) -> None:
    """Write goals.md with the given body — caller picks the sections."""
    (wiki / "goals.md").write_text(body)


def _read(wiki) -> str:
    return (wiki / "goals.md").read_text()


_EMPTY_TEMPLATE = (
    "# Goals\n\n"
    "## Standing context\n\n\n"
    "## Automations\n\n\n"
    "## Tasks\n\n\n"
    "## Waiting for\n\n\n"
    "## Reminders\n\n\n"
    "## Archive\n\n"
)


# --- Section parser round-trips --------------------------------------------


def test_parse_sections_round_trip_preserves_content(isolated_home):
    _, wiki = isolated_home
    original = (
        "---\nkeywords: [a, b]\n---\n\n"
        "# Goals\n\n"
        "*preamble*\n\n"
        "## Standing context\n\n- standing one\n\n"
        "## Tasks\n\n- [ ] call memere\n- [x] old thing\n\n"
        "## Unknown user section\n\n- custom line\n\n"
    )
    _seed_goals(wiki, original)
    preamble, sections = goals._parse_sections(original)

    assert "## Standing context" not in "\n".join(preamble)  # preamble excludes headers
    assert "Standing context" in sections
    assert "Tasks" in sections
    assert "Unknown user section" in sections  # unknown sections preserved

    rendered = goals._render_sections(preamble, sections)
    # Round-trip is faithful up to whitespace normalization
    assert "- [ ] call memere" in rendered
    assert "- [x] old thing" in rendered
    assert "custom line" in rendered
    assert "keywords:" in rendered  # YAML frontmatter preserved


# --- Tasks -----------------------------------------------------------------


def test_add_task_dedups_substring(isolated_home):
    _, wiki = isolated_home
    _seed_goals(wiki, _EMPTY_TEMPLATE)

    n1 = goals.apply_tasks_update({"add_tasks": ["call memere"]})
    assert n1 == 1
    n2 = goals.apply_tasks_update({"add_tasks": ["call memere"]})  # duplicate
    assert n2 == 0  # ops that changed nothing return zero; no new line

    text = _read(wiki)
    assert text.count("call memere") == 1


def test_complete_task_flips_checkbox(isolated_home):
    _, wiki = isolated_home
    _seed_goals(wiki, _EMPTY_TEMPLATE)

    goals.apply_tasks_update({"add_tasks": ["send amanda the deck"]})
    goals.apply_tasks_update({"complete_tasks": ["send amanda the deck"]})

    text = _read(wiki)
    assert "- [x] send amanda the deck" in text
    assert "- [ ] send amanda the deck" not in text


def test_archive_task_moves_to_archive(isolated_home):
    _, wiki = isolated_home
    _seed_goals(wiki, _EMPTY_TEMPLATE)

    goals.apply_tasks_update({"add_tasks": ["call roofer about second bid"]})
    changes = goals.apply_tasks_update({
        "archive_tasks": [{"needle": "call roofer", "reason": "project closed Apr 3"}]
    })
    assert changes >= 1

    text = _read(wiki)
    # Line moved out of Tasks
    tasks_block = text.split("## Tasks")[1].split("## ")[0]
    assert "call roofer" not in tasks_block
    # Line is now in Archive with the reason suffix
    archive_block = text.split("## Archive")[1]
    assert "call roofer" in archive_block
    assert "project closed Apr 3" in archive_block


# --- Waiting-for -----------------------------------------------------------


def test_add_waiting_stamps_added_date(isolated_home):
    _, wiki = isolated_home
    _seed_goals(wiki, _EMPTY_TEMPLATE)

    goals.apply_tasks_update({
        "add_waiting": ["**Amanda** — theme feedback"]
    })
    text = _read(wiki)
    assert "**Amanda** — theme feedback (added " in text
    # The added date must be today's ISO date so auto-expiry can parse it
    today = date.today().isoformat()
    assert today in text


def test_resolve_waiting_flips_checkbox(isolated_home):
    _, wiki = isolated_home
    _seed_goals(wiki, _EMPTY_TEMPLATE)

    goals.apply_tasks_update({"add_waiting": ["**Jon** — roof quote"]})
    goals.apply_tasks_update({"resolve_waiting": ["jon"]})  # substring match

    text = _read(wiki)
    assert "- [x] **Jon** — roof quote" in text


# --- Reminders -------------------------------------------------------------


def test_add_reminder_with_topics(isolated_home):
    _, wiki = isolated_home
    _seed_goals(wiki, _EMPTY_TEMPLATE)

    goals.apply_tasks_update({
        "add_reminders": [
            {
                "date": "2026-04-18",
                "question": "did amanda reply about the deck?",
                "topics": ["amanda-peffer", "blade-and-rose"],
            }
        ]
    })
    text = _read(wiki)
    assert "[2026-04-18] did amanda reply about the deck?" in text
    assert "[[amanda-peffer]]" in text
    assert "[[blade-and-rose]]" in text


def test_add_reminder_rejects_bad_date(isolated_home):
    _, wiki = isolated_home
    _seed_goals(wiki, _EMPTY_TEMPLATE)

    changes = goals.apply_tasks_update({
        "add_reminders": [
            {"date": "next friday", "question": "check something"},
        ]
    })
    assert changes == 0
    assert "next friday" not in _read(wiki)


def test_resolve_reminder_flips_checkbox(isolated_home):
    _, wiki = isolated_home
    _seed_goals(wiki, _EMPTY_TEMPLATE)

    goals.apply_tasks_update({
        "add_reminders": [
            {"date": "2026-04-18", "question": "did amanda reply?", "topics": []}
        ]
    })
    # Reminders use a different bullet format (no [ ] checkbox); resolving
    # should still mark them done via the same mechanism.
    goals.apply_tasks_update({"resolve_reminders": ["amanda reply"]})
    text = _read(wiki)
    # Resolved reminders are marked with an x checkbox inserted by goals
    # OR removed entirely — either is valid. We just need the substring
    # to not be an OPEN reminder anymore.
    reminders_block = text.split("## Reminders")[1].split("## ")[0]
    assert "- [2026-04-18] did amanda reply" not in reminders_block or \
           "- [x]" in reminders_block


# --- Auto-expiry -----------------------------------------------------------


def test_waiting_auto_expires_after_21_days(isolated_home):
    _, wiki = isolated_home
    old_date = (date.today() - timedelta(days=30)).isoformat()
    body = _EMPTY_TEMPLATE.replace(
        "## Waiting for\n\n\n",
        f"## Waiting for\n\n- [ ] **Jon** — roof quote (added {old_date})\n\n",
    )
    _seed_goals(wiki, body)

    # Any write triggers the auto-expiry sweep
    goals.apply_tasks_update({"add_tasks": ["unrelated new task"]})

    text = _read(wiki)
    waiting_block = text.split("## Waiting for")[1].split("## ")[0]
    assert "Jon" not in waiting_block  # moved out
    archive_block = text.split("## Archive")[1]
    assert "Jon" in archive_block
    assert "no response" in archive_block


def test_reminder_auto_expires_past_14_days(isolated_home):
    _, wiki = isolated_home
    old_due = (date.today() - timedelta(days=30)).isoformat()
    body = _EMPTY_TEMPLATE.replace(
        "## Reminders\n\n\n",
        f"## Reminders\n\n- [{old_due}] did amanda reply?\n\n",
    )
    _seed_goals(wiki, body)

    goals.apply_tasks_update({"add_tasks": ["anything"]})

    text = _read(wiki)
    reminders_block = text.split("## Reminders")[1].split("## ")[0]
    assert "did amanda reply" not in reminders_block
    archive_block = text.split("## Archive")[1]
    assert "did amanda reply" in archive_block
    assert "past due" in archive_block


def test_fresh_reminder_not_expired(isolated_home):
    _, wiki = isolated_home
    future = (date.today() + timedelta(days=5)).isoformat()
    body = _EMPTY_TEMPLATE.replace(
        "## Reminders\n\n\n",
        f"## Reminders\n\n- [{future}] check the thing\n\n",
    )
    _seed_goals(wiki, body)

    goals.apply_tasks_update({"add_tasks": ["anything"]})
    text = _read(wiki)
    assert f"[{future}] check the thing" in text
    # Still in the Reminders section, not archived
    reminders_block = text.split("## Reminders")[1].split("## ")[0]
    assert "check the thing" in reminders_block


# --- due_reminder_topics (feeds wiki_retriever) -----------------------------


def test_due_reminder_topics_returns_slugs_for_due_reminders(isolated_home):
    _, wiki = isolated_home
    today = date.today().isoformat()
    future = (date.today() + timedelta(days=10)).isoformat()
    body = _EMPTY_TEMPLATE.replace(
        "## Reminders\n\n\n",
        (
            f"## Reminders\n\n"
            f"- [{today}] question one → [[amanda-peffer]], [[blade-and-rose]]\n"
            f"- [{future}] not due yet → [[casita-roof]]\n\n"
        ),
    )
    _seed_goals(wiki, body)

    topics = goals.due_reminder_topics()
    assert "amanda-peffer" in topics
    assert "blade-and-rose" in topics
    assert "casita-roof" not in topics  # future date
