"""Side-by-side comparison of the snippet-era vs full-body email rendering.

Takes one Gmail thread id (or pulls the most recent few sent threads) and
prints:
  1. The OLD snippet-based rendering (for reference)
  2. The NEW full-body + stripped + consolidated rendering

Usage:
    uv run python tools/email_thread_compare.py                # pulls 3 recent threads
    uv run python tools/email_thread_compare.py <thread_id>    # single thread

Read-only — no observations written, no cursor advanced.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from deja.observations.email import (  # noqa: E402
    _build_observation_from_thread,
    _get_thread,
)


def _render_old_snippet_style(thread_messages: list[dict]) -> str:
    """Reproduce the pre-change snippet-based rendering for comparison."""
    subject = ""
    thread_lines: list[str] = []
    for tm in thread_messages:
        hdrs = {h.get("name"): h.get("value", "")
                for h in tm.get("payload", {}).get("headers", [])}
        if not subject:
            subject = hdrs.get("Subject", "")
        frm = hdrs.get("From", "")
        to = hdrs.get("To", "")
        date = hdrs.get("Date", "")
        snip = (tm.get("snippet") or "").replace("&#39;", "'").replace("&amp;", "&").replace("&quot;", '"')
        thread_lines.append(f"  {frm} → {to} ({date[:25]}): {snip[:250]}")
    n = len(thread_messages)
    if n == 1:
        return f"{subject} — {thread_lines[0].strip()}"
    return f"EMAIL THREAD ({n} messages) — {subject}\n" + "\n".join(thread_lines)


def _list_recent_thread_ids(n: int = 3) -> list[str]:
    result = subprocess.run(
        [
            "gws", "gmail", "users", "threads", "list",
            "--params", json.dumps({"userId": "me", "maxResults": n, "q": "in:sent newer_than:14d"}),
            "--format", "json",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        print(f"gws failed: {result.stderr[:200]}")
        return []
    data = json.loads(result.stdout)
    return [t["id"] for t in data.get("threads", [])]


def _compare_one(thread_id: str) -> None:
    print("=" * 80)
    print(f"THREAD {thread_id}")
    print("=" * 80)

    thread_messages = _get_thread(thread_id)
    if not thread_messages:
        print("  (empty / fetch failed)")
        return

    print(f"\n{len(thread_messages)} message(s) in thread\n")

    old = _render_old_snippet_style(thread_messages)
    print("--- OLD (snippet, 250-char/msg) ---")
    print(old)
    print(f"\n  OLD size: {len(old)} chars\n")

    obs = _build_observation_from_thread(thread_messages[-1].get("id", "unknown"),
                                         thread_messages, "outgoing")
    new_text = obs.text if obs else "(no observation produced)"
    print("--- NEW (full body, stripped, consolidated if long) ---")
    print(new_text)
    print(f"\n  NEW size: {len(new_text)} chars")
    print(f"  Ratio: {len(new_text) / max(1, len(old)):.2f}x")
    print()


def main() -> None:
    args = sys.argv[1:]
    if args:
        thread_ids = args
    else:
        print("No thread id given — pulling 3 most recent sent threads\n")
        thread_ids = _list_recent_thread_ids(3)
    if not thread_ids:
        print("No threads to compare")
        sys.exit(1)
    for tid in thread_ids:
        _compare_one(tid)


if __name__ == "__main__":
    main()
