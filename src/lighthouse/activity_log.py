"""Human-readable append-only log for Lighthouse activity.

Writes one-line bullet entries to `~/Lighthouse/log.md` so David
can browse what the agent has done directly in Obsidian alongside the rest of
the vault. Complements (does not replace) the machine-readable
`~/.lighthouse/integrations.jsonl` audit log.

Format (kept stable so `grep '^- \\*\\*\\['` works):

    - **[YYYY-MM-DD HH:MM]** entry_type — one-line summary

Entries are appended (newest at the bottom), each as a single markdown bullet
so the file reads cleanly in Obsidian's reading view. All operations are
best-effort and swallow exceptions -- logging is never on the critical path.
"""

from lighthouse.config import WIKI_DIR
from datetime import datetime
from pathlib import Path

LOG_PATH = WIKI_DIR / "log.md"

_PREAMBLE = (
    "# Log\n"
    "\n"
    "*Append-only record of what the agent has done to the wiki. "
    "Newest entries at the bottom.*\n"
    "\n"
)

_ENTRY_PREFIX = "- **["


def append_log_entry(entry_type: str, summary: str) -> None:
    """Append one bullet line to the wiki's log.md.

    entry_type: short identifier like 'cycle', 'nightly', 'chat', 'manual'
    summary:    one-line description (multi-line input is collapsed).

    Creates log.md with a preamble on first use. Best-effort: swallows all
    exceptions (logging is not critical path).
    """
    try:
        one_line_summary = " ".join((summary or "").split())
        safe_entry_type = " ".join((entry_type or "").split()) or "unknown"
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

        if not LOG_PATH.exists():
            LOG_PATH.write_text(_PREAMBLE, encoding="utf-8")

        line = f"{_ENTRY_PREFIX}{timestamp}]** {safe_entry_type} — {one_line_summary}\n"

        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:
        pass


def read_recent_log(max_entries: int = 20) -> str:
    """Return the last N log entries as a single string, for LLM context."""
    try:
        if not LOG_PATH.exists():
            return ""
        text = LOG_PATH.read_text(encoding="utf-8")
    except Exception:
        return ""

    try:
        lines = [ln for ln in text.splitlines() if ln.startswith(_ENTRY_PREFIX)]
        if not lines:
            return ""
        return "\n".join(lines[-max_entries:])
    except Exception:
        return ""
