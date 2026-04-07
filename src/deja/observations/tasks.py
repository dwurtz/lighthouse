"""Google Tasks signal collector using the gws CLI tool."""

from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime

from deja.observations.types import Observation

log = logging.getLogger(__name__)

TASKLIST_ID = "MDcyMDk1NzkwNzU5MDEzNzE5MTk6MDow"


def collect_pending_tasks() -> list[Observation]:
    """Collect incomplete Google Tasks using gws CLI."""
    signals: list[Observation] = []

    try:
        result = subprocess.run(
            [
                "gws", "tasks", "tasks", "list",
                "--params", json.dumps({
                    "tasklist": TASKLIST_ID,
                    "showCompleted": False,
                }),
                "--format", "json",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )

        if result.returncode != 0:
            log.warning("gws tasks list failed: %s", result.stderr[:200])
            return signals

        data = json.loads(result.stdout)
        items = data.get("items", [])

        for task in items:
            task_id = task.get("id")
            title = task.get("title", "").strip()
            if not task_id or not title:
                continue

            text = f"Task: {title}"
            due = task.get("due")
            if due:
                # due is typically an RFC 3339 date string
                try:
                    due_dt = datetime.fromisoformat(due.replace("Z", "+00:00"))
                    text += f" (due {due_dt.strftime('%Y-%m-%d')})"
                except (ValueError, TypeError):
                    text += f" (due {due})"

            signals.append(Observation(
                source="tasks",
                sender="Google Tasks",
                text=text[:500],
                timestamp=datetime.now(),
                id_key=f"task-{task_id}",
            ))

    except subprocess.TimeoutExpired:
        log.warning("gws tasks list timed out")
    except json.JSONDecodeError:
        log.warning("gws tasks list returned invalid JSON")
    except FileNotFoundError:
        log.warning("gws CLI not found on PATH")
    except Exception:
        log.exception("Tasks collector error")

    return signals
