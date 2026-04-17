"""Tests for the goals_reconcile sweep.

Covers:
  - Parsing open (unchecked) waiting-for bullets from goals.md, ignoring
    preamble / completed rows / trailing Archive.
  - Loading event pages from the last 48 hours out of
    ~/Deja/events/YYYY-MM-DD/.
  - Mocked Flash confirm → satisfied items flip to - [x] in goals.md +
    goals_reconcile_resolved audit entries are written.
  - No-op when there are no recent events.
  - No-op when there are no open waiting-fors.
  - Indirect satisfaction: Jon Sturos waiting_for + Davin Tarnanen event
    that references Jon as referrer → Flash returns satisfied → line is
    resolved.
  - Non-match: a waiting_for whose subject isn't in any event → Flash
    returns unsatisfied → nothing changes.

The confirm step is mocked throughout so tests run offline.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from deja import goals_reconcile as gr


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_EMPTY_GOALS = (
    "# Goals\n\n"
    "## Standing context\n\n\n"
    "## Automations\n\n\n"
    "## Tasks\n\n\n"
    "## Waiting for\n\n{waiting}\n\n"
    "## Reminders\n\n\n"
    "## Archive\n\n- [ ] **Archived Item** — archived 2026-01-01: stale\n"
)


def _seed_goals(wiki: Path, waiting_lines: list[str]) -> None:
    waiting = "\n".join(waiting_lines) if waiting_lines else ""
    (wiki / "goals.md").write_text(_EMPTY_GOALS.format(waiting=waiting))


def _seed_event(
    wiki: Path,
    day: str,
    slug: str,
    *,
    people: list[str],
    projects: list[str] | None = None,
    title: str | None = None,
    body: str = "Event body.",
) -> str:
    day_dir = wiki / "events" / day
    day_dir.mkdir(parents=True, exist_ok=True)
    page = day_dir / f"{slug}.md"
    projects = projects or []
    people_str = "[" + ", ".join(people) + "]"
    projects_str = "[" + ", ".join(projects) + "]"
    page.write_text(
        f"---\n"
        f"date: {day}\n"
        f'time: "10:00"\n'
        f"people: {people_str}\n"
        f"projects: {projects_str}\n"
        f"---\n"
        f"# {title or slug.replace('-', ' ').title()}\n\n"
        f"{body}\n"
    )
    return f"events/{day}/{slug}.md"


def _patch_flash(monkeypatch, resolutions: list[dict]) -> None:
    """Patch GeminiClient._generate_full to return canned resolutions."""
    payload = {
        "text": json.dumps({"resolutions": resolutions}),
        "usage_metadata": {
            "prompt_token_count": 800,
            "candidates_token_count": 200,
            "thoughts_token_count": 0,
        },
    }

    async def fake_generate_full(self, model, contents, config_dict):
        return payload

    from deja.llm_client import GeminiClient
    monkeypatch.setattr(GeminiClient, "_generate_full", fake_generate_full)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_parse_open_waiting_from_goals_returns_unchecked_only(isolated_home):
    _, wiki = isolated_home
    _seed_goals(
        wiki,
        [
            "- [ ] **Jon Sturos** — builder contact for detached garage (added 2026-04-14)",
            "- [x] **Amanda** — theme feedback (added 2026-04-01)",
            "- [ ] **Roofer** — second bid (added 2026-04-10)",
        ],
    )
    items = gr._parse_open_waiting_from_goals()
    assert len(items) == 2
    texts = [it.text for it in items]
    assert any("Jon Sturos" in t for t in texts)
    assert any("Roofer" in t for t in texts)
    # Needles should be distinctive substrings present verbatim in the line.
    for it in items:
        # strip the (added ...) suffix for the verbatim check since our
        # needle builder drops it — but the head is still in the raw line.
        head = it.needle.split(" — ")[0]
        assert head in it.raw_line


def test_parse_ignores_archive_section(isolated_home):
    """Archive section items (even if - [ ]) are not waiting-fors."""
    _, wiki = isolated_home
    _seed_goals(wiki, [])
    items = gr._parse_open_waiting_from_goals()
    assert items == []


def test_parse_returns_empty_when_goals_missing(isolated_home, monkeypatch):
    _, wiki = isolated_home
    # Do not create goals.md
    items = gr._parse_open_waiting_from_goals()
    assert items == []


# ---------------------------------------------------------------------------
# Event loading
# ---------------------------------------------------------------------------


def test_load_recent_events_within_window(isolated_home):
    _, wiki = isolated_home
    today = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    ancient = (date.today() - timedelta(days=30)).isoformat()
    _seed_event(wiki, today, "todayish", people=["a"], projects=[])
    _seed_event(wiki, yesterday, "yesterdayish", people=["b"], projects=[])
    _seed_event(wiki, ancient, "too-old", people=["c"], projects=[])

    events = gr._load_recent_events(days=2)
    paths = {e.path for e in events}
    assert any("todayish" in p for p in paths)
    assert any("yesterdayish" in p for p in paths)
    assert not any("too-old" in p for p in paths)


def test_load_recent_events_empty_directory(isolated_home):
    _, wiki = isolated_home
    events = gr._load_recent_events(days=2)
    assert events == []


# ---------------------------------------------------------------------------
# run_goals_reconcile — no-op paths
# ---------------------------------------------------------------------------


def test_noop_when_no_open_waiting(isolated_home, monkeypatch):
    _, wiki = isolated_home
    _seed_goals(wiki, [])  # no open waiting_for

    # Even with recent events, nothing to do.
    _seed_event(
        wiki, date.today().isoformat(), "some-event",
        people=["someone"],
    )

    # Flash should never be called — fail loudly if it is.
    async def boom(self, model, contents, config_dict):
        raise AssertionError("Flash must not be called when no open waiting-fors")

    from deja.llm_client import GeminiClient
    monkeypatch.setattr(GeminiClient, "_generate_full", boom)

    result = asyncio.run(gr.run_goals_reconcile())
    assert result["open_waiting"] == 0
    assert result["resolved"] == 0


def test_noop_when_no_recent_events(isolated_home, monkeypatch):
    _, wiki = isolated_home
    _seed_goals(
        wiki,
        ["- [ ] **Jon** — roof quote (added 2026-04-10)"],
    )
    # No events seeded anywhere.

    async def boom(self, model, contents, config_dict):
        raise AssertionError("Flash must not be called when no recent events")

    from deja.llm_client import GeminiClient
    monkeypatch.setattr(GeminiClient, "_generate_full", boom)

    result = asyncio.run(gr.run_goals_reconcile())
    assert result["open_waiting"] == 1
    assert result["recent_events"] == 0
    assert result["resolved"] == 0
    # goals.md should be untouched.
    text = (wiki / "goals.md").read_text()
    assert "- [ ] **Jon** — roof quote" in text


# ---------------------------------------------------------------------------
# run_goals_reconcile — full mocked flow
# ---------------------------------------------------------------------------


def test_satisfied_item_flips_checkbox_and_writes_audit(isolated_home, monkeypatch):
    """Mock Flash to return satisfied → goals.md line flips to - [x]."""
    home, wiki = isolated_home
    _seed_goals(
        wiki,
        ["- [ ] **Jon Sturos** — builder contact for detached garage (added 2026-04-14)"],
    )
    today = date.today().isoformat()
    _seed_event(
        wiki, today, "davin-intro",
        people=["davin-tarnanen", "david-wurtz", "jon-sturos"],
        projects=["detached-garage"],
        title="Davin Tarnanen email re detached garage",
        body=(
            "Davin emailed David — 'Jon Sturos referred me. "
            "I build detached garages in Fairfield; happy to quote.'"
        ),
    )

    _patch_flash(
        monkeypatch,
        [
            {
                "needle": "**Jon Sturos** — builder contact",
                "satisfied": True,
                "reason": (
                    "Davin Tarnanen emailed David, explicitly citing Jon "
                    "Sturos as the referrer — Jon's commitment "
                    "'send builder contact' is fulfilled."
                ),
            }
        ],
    )

    result = asyncio.run(gr.run_goals_reconcile())
    assert result["open_waiting"] == 1
    assert result["recent_events"] >= 1
    assert result["resolved"] >= 1

    text = (wiki / "goals.md").read_text()
    assert "- [x] **Jon Sturos** — builder contact for detached garage" in text

    # Audit: a goals_reconcile_resolved row + goals.py's waiting_resolve row.
    audit_path = home / "audit.jsonl"
    assert audit_path.exists()
    audit_lines = [
        json.loads(ln) for ln in audit_path.read_text().splitlines() if ln.strip()
    ]
    assert any(e.get("action") == "goals_reconcile_resolved" for e in audit_lines)
    assert any(e.get("action") == "waiting_resolve" for e in audit_lines)


def test_unsatisfied_item_does_not_change_goals(isolated_home, monkeypatch):
    _, wiki = isolated_home
    _seed_goals(
        wiki,
        ["- [ ] **Maya** — Q3 budget draft (added 2026-04-14)"],
    )
    today = date.today().isoformat()
    _seed_event(
        wiki, today, "unrelated-thread",
        people=["bob"],
        title="Bob pinged about weather",
        body="Totally unrelated conversation.",
    )

    _patch_flash(
        monkeypatch,
        [
            {
                "needle": "**Maya** — Q3 budget draft",
                "satisfied": False,
                "reason": "No event references Maya or the Q3 budget.",
            }
        ],
    )

    result = asyncio.run(gr.run_goals_reconcile())
    assert result["open_waiting"] == 1
    assert result["resolved"] == 0

    text = (wiki / "goals.md").read_text()
    assert "- [ ] **Maya** — Q3 budget draft" in text
    assert "- [x] **Maya**" not in text


def test_mixed_batch_satisfied_and_not(isolated_home, monkeypatch):
    """One satisfied, one not → only the satisfied one flips."""
    _, wiki = isolated_home
    _seed_goals(
        wiki,
        [
            "- [ ] **Jon Sturos** — builder contact for detached garage (added 2026-04-14)",
            "- [ ] **Maya** — Q3 budget draft (added 2026-04-14)",
        ],
    )
    today = date.today().isoformat()
    _seed_event(
        wiki, today, "davin",
        people=["davin-tarnanen", "jon-sturos"],
        title="Davin email",
        body="Davin wrote: 'Jon Sturos referred me about the detached garage.'",
    )

    _patch_flash(
        monkeypatch,
        [
            {
                "needle": "**Jon Sturos** — builder contact",
                "satisfied": True,
                "reason": "Davin's email, referred by Jon — fulfilled.",
            },
            {
                "needle": "**Maya** — Q3 budget draft",
                "satisfied": False,
                "reason": "No Maya event in the window.",
            },
        ],
    )

    result = asyncio.run(gr.run_goals_reconcile())
    assert result["open_waiting"] == 2
    assert result["resolved"] == 1

    text = (wiki / "goals.md").read_text()
    assert "- [x] **Jon Sturos** — builder contact for detached garage" in text
    assert "- [ ] **Maya** — Q3 budget draft" in text


def test_coverage_check_raises_when_flash_drops_items(isolated_home, monkeypatch):
    """Flash omits an open item → run raises, marker isn't advanced."""
    _, wiki = isolated_home
    _seed_goals(
        wiki,
        [
            "- [ ] **First** — owed thing (added 2026-04-14)",
            "- [ ] **Second** — another owed thing (added 2026-04-14)",
        ],
    )
    today = date.today().isoformat()
    _seed_event(wiki, today, "e1", people=["x"], body="hi")

    # Only returns one resolution; second item is silently dropped.
    _patch_flash(
        monkeypatch,
        [
            {
                "needle": "**First** — owed thing",
                "satisfied": False,
                "reason": "no match",
            }
        ],
    )

    with pytest.raises(RuntimeError, match="omitted"):
        asyncio.run(gr.run_goals_reconcile())


# ---------------------------------------------------------------------------
# Task completion path — user-as-actor
# ---------------------------------------------------------------------------


_GOALS_WITH_TASKS = (
    "# Goals\n\n"
    "## Standing context\n\n\n"
    "## Automations\n\n\n"
    "## Tasks\n\n{tasks}\n\n"
    "## Waiting for\n\n{waiting}\n\n"
    "## Reminders\n\n\n"
    "## Archive\n\n"
)


def _seed_goals_tasks_and_waiting(
    wiki: Path,
    tasks: list[str],
    waiting: list[str],
) -> None:
    tasks_block = "\n".join(tasks) if tasks else ""
    waiting_block = "\n".join(waiting) if waiting else ""
    (wiki / "goals.md").write_text(
        _GOALS_WITH_TASKS.format(tasks=tasks_block, waiting=waiting_block)
    )


def test_parse_returns_tasks_and_waiting(isolated_home):
    """Parser surfaces both kinds and labels them correctly."""
    _, wiki = isolated_home
    _seed_goals_tasks_and_waiting(
        wiki,
        tasks=[
            "- [ ] send Amanda the revised deck",
            "- [x] already done task",
        ],
        waiting=[
            "- [ ] **Jon** — builder contact (added 2026-04-14)",
        ],
    )
    items = gr._parse_open_items_from_goals()
    kinds = {it.kind for it in items}
    assert kinds == {"task", "waiting"}
    task_items = [it for it in items if it.kind == "task"]
    waiting_items = [it for it in items if it.kind == "waiting"]
    assert len(task_items) == 1
    assert "send Amanda" in task_items[0].text
    assert len(waiting_items) == 1
    assert "Jon" in waiting_items[0].text


def test_satisfied_task_flips_checkbox(isolated_home, monkeypatch):
    """Mock Flash yes on a task → Tasks section line flips to - [x]."""
    _, wiki = isolated_home
    _seed_goals_tasks_and_waiting(
        wiki,
        tasks=["- [ ] send Amanda the revised deck"],
        waiting=[],
    )
    today = date.today().isoformat()
    _seed_event(
        wiki, today, "sent-deck",
        people=["amanda-peffer", "david-wurtz"],
        projects=["amanda-deck"],
        title="Sent Amanda the revised deck",
        body=(
            "David sent Amanda the revised deck this morning with the "
            "new section on growth loops attached."
        ),
    )

    _patch_flash(
        monkeypatch,
        [
            {
                "needle": "send Amanda the revised deck",
                "kind": "task",
                "satisfied": True,
                "reason": "Sent-deck event shows David actually sent it.",
            }
        ],
    )

    result = asyncio.run(gr.run_goals_reconcile())
    assert result["open_tasks"] == 1
    assert result["tasks_completed"] == 1
    assert result["waiting_resolved"] == 0
    assert result["resolved"] == 1

    text = (wiki / "goals.md").read_text()
    assert "- [x] send Amanda the revised deck" in text
    assert "- [ ] send Amanda the revised deck" not in text


def test_mixed_task_and_waiting_batch(isolated_home, monkeypatch):
    """One task + one waiting, both satisfied → both flip, routed correctly."""
    home, wiki = isolated_home
    _seed_goals_tasks_and_waiting(
        wiki,
        tasks=["- [ ] book the Portland flights"],
        waiting=[
            "- [ ] **Jon Sturos** — builder contact for detached garage (added 2026-04-14)",
        ],
    )
    today = date.today().isoformat()
    _seed_event(
        wiki, today, "flight-booked",
        people=["david-wurtz"],
        title="Booked Portland flights",
        body="David booked SFO→PDX roundtrip for April 22–24.",
    )
    _seed_event(
        wiki, today, "davin-intro",
        people=["davin-tarnanen", "jon-sturos", "david-wurtz"],
        projects=["detached-garage"],
        title="Davin Tarnanen intro",
        body="Davin wrote: 'Jon Sturos asked me to reach out about the garage.'",
    )

    _patch_flash(
        monkeypatch,
        [
            {
                "needle": "book the Portland flights",
                "kind": "task",
                "satisfied": True,
                "reason": "Flight-booked event shows David booked SFO->PDX.",
            },
            {
                "needle": "**Jon Sturos** — builder contact",
                "kind": "waiting",
                "satisfied": True,
                "reason": "Davin Tarnanen emailed citing Jon as referrer.",
            },
        ],
    )

    result = asyncio.run(gr.run_goals_reconcile())
    assert result["open_tasks"] == 1
    assert result["open_waiting"] == 1
    assert result["tasks_completed"] == 1
    assert result["waiting_resolved"] == 1
    assert result["resolved"] == 2

    text = (wiki / "goals.md").read_text()
    assert "- [x] book the Portland flights" in text
    assert "- [x] **Jon Sturos** — builder contact for detached garage" in text

    # Both kinds produce audit trails: goals.py emits task_complete +
    # waiting_resolve, and goals_reconcile adds one extra row per
    # satisfied item.
    audit_path = home / "audit.jsonl"
    lines = [
        json.loads(ln)
        for ln in audit_path.read_text().splitlines()
        if ln.strip()
    ]
    actions = [e.get("action") for e in lines]
    assert "task_complete" in actions
    assert "waiting_resolve" in actions
    resolved_rows = [e for e in lines if e.get("action") == "goals_reconcile_resolved"]
    assert len(resolved_rows) == 2
    targets = {e.get("target", "") for e in resolved_rows}
    assert any(t.startswith("tasks/") for t in targets)
    assert any(t.startswith("waiting_for/") for t in targets)


def test_legacy_parse_open_waiting_shim_still_works(isolated_home):
    """The legacy _parse_open_waiting_from_goals shim returns only waitings."""
    _, wiki = isolated_home
    _seed_goals_tasks_and_waiting(
        wiki,
        tasks=["- [ ] a task"],
        waiting=["- [ ] **Name** — a waiting (added 2026-04-14)"],
    )
    legacy = gr._parse_open_waiting_from_goals()
    assert len(legacy) == 1
    assert legacy[0].kind == "waiting"
