"""Goals manager — agent-maintained task list and waiting-for tracker.

goals.md has two kinds of sections:
  - USER-MANAGED: Standing context, Automations, Recurring — the user
    edits these in Obsidian, the agent only reads them.
  - AGENT-MANAGED: Tasks, Waiting for — the agent adds items when it
    observes commitments and resolves them when it sees completion signals.

This module handles the safe modification of goals.md — it reads the
file, applies structured changes (add/complete tasks, add/resolve
waiting-for items) to only the agent-managed sections, and writes it
back without touching the user-managed sections.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from deja.config import WIKI_DIR

log = logging.getLogger(__name__)

GOALS_PATH = WIKI_DIR / "goals.md"


def apply_tasks_update(update: dict) -> int:
    """Apply structured changes to goals.md Tasks and Waiting For sections.

    The ``update`` dict can have:
      - add_tasks: list[str] — new task items to add
      - complete_tasks: list[str] — task descriptions to mark as done [x]
      - add_waiting: list[str] — new waiting-for items to add
      - resolve_waiting: list[str] — waiting-for items to mark as done [x]

    Returns the number of changes applied. Never touches Standing context,
    Automations, or Recurring sections.
    """
    if not GOALS_PATH.exists():
        log.warning("goals.md not found at %s", GOALS_PATH)
        return 0

    add_tasks = update.get("add_tasks") or []
    complete_tasks = update.get("complete_tasks") or []
    add_waiting = update.get("add_waiting") or []
    resolve_waiting = update.get("resolve_waiting") or []

    if not any([add_tasks, complete_tasks, add_waiting, resolve_waiting]):
        return 0

    text = GOALS_PATH.read_text(encoding="utf-8")
    changes = 0

    # --- Add tasks ---
    for task in add_tasks:
        task = task.strip()
        if not task:
            continue
        # Don't add duplicates (case-insensitive substring match)
        if task.lower() in text.lower():
            log.debug("goals: task already exists, skipping: %s", task[:60])
            continue
        # Insert before the "## Waiting for" section (i.e., at the end of Tasks)
        marker = "## Waiting for"
        if marker in text:
            text = text.replace(marker, f"- [ ] {task}\n\n{marker}")
        else:
            # Fallback: append to end of file
            text = text.rstrip() + f"\n- [ ] {task}\n"
        changes += 1
        log.info("goals: added task: %s", task[:80])

    # --- Complete tasks ---
    for task_desc in complete_tasks:
        task_desc = task_desc.strip().lower()
        if not task_desc:
            continue
        # Find unchecked task lines that match the description
        lines = text.split("\n")
        for i, line in enumerate(lines):
            if line.strip().startswith("- [ ]") and task_desc in line.lower():
                lines[i] = line.replace("- [ ]", "- [x]", 1)
                changes += 1
                log.info("goals: completed task: %s", line.strip()[:80])
                break
        text = "\n".join(lines)

    # --- Add waiting-for ---
    for item in add_waiting:
        item = item.strip()
        if not item:
            continue
        if item.lower() in text.lower():
            log.debug("goals: waiting-for already exists, skipping: %s", item[:60])
            continue
        # Insert before the "## Recurring" section
        marker = "## Recurring"
        if marker in text:
            text = text.replace(marker, f"- [ ] {item}\n\n{marker}")
        else:
            text = text.rstrip() + f"\n- [ ] {item}\n"
        changes += 1
        log.info("goals: added waiting-for: %s", item[:80])

    # --- Resolve waiting-for ---
    for item_desc in resolve_waiting:
        item_desc = item_desc.strip().lower()
        if not item_desc:
            continue
        lines = text.split("\n")
        for i, line in enumerate(lines):
            if line.strip().startswith("- [ ]") and item_desc in line.lower():
                lines[i] = line.replace("- [ ]", "- [x]", 1)
                changes += 1
                log.info("goals: resolved waiting-for: %s", line.strip()[:80])
                break
        text = "\n".join(lines)

    if changes:
        GOALS_PATH.write_text(text, encoding="utf-8")
        try:
            from deja.activity_log import append_log_entry
            append_log_entry("goals", f"updated {changes} item(s) in goals.md")
        except Exception:
            pass

    return changes


def append_to_automations_section(rule_text: str) -> None:
    """Append a user-authored automation rule to goals.md ## Automations.

    Preserves the existing content of the section. Inserts the new bullet
    immediately before the next "## " heading so rules stay grouped.

    Raises RuntimeError if goals.md is missing or does not contain an
    ``## Automations`` heading — the user-managed sections are not
    auto-created; a clear error beats silently inventing structure.
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

    # Find the ## Automations heading and the next "## " heading after it.
    marker = "## Automations"
    idx = text.find(marker)
    if idx == -1:
        raise RuntimeError(
            "goals.md is missing the '## Automations' section. Add it "
            "manually (between ## Standing context and ## Tasks) and "
            "retry — the agent will not auto-create user-managed sections."
        )

    # Dedup — if the rule already exists, skip silently.
    if rule_text.lower() in text.lower():
        log.info("automation: rule already in goals.md, skipping: %s", rule_text[:80])
        return

    # Find insertion point: just before the next "## " heading after Automations.
    search_from = idx + len(marker)
    next_heading_idx = text.find("\n## ", search_from)
    insert_line = f"- {rule_text}\n"

    if next_heading_idx == -1:
        # Automations is the last section — append at EOF.
        new_text = text.rstrip() + "\n" + insert_line
    else:
        # Insert right before the next "## " heading, separated by a blank line.
        head = text[: next_heading_idx + 1]  # include the leading \n
        tail = text[next_heading_idx + 1 :]
        # Ensure there's a blank line before the new bullet and after
        new_text = head.rstrip() + "\n" + insert_line + "\n" + tail

    GOALS_PATH.write_text(new_text, encoding="utf-8")
    log.info("automation: added rule to goals.md: %s", rule_text[:80])

    try:
        from deja.activity_log import append_log_entry
        append_log_entry("goals", f"added automation: {rule_text[:120]}")
    except Exception:
        pass
