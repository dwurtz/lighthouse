"""Collect open Chrome tabs via Chrome DevTools Protocol and AppleScript fallback."""

from __future__ import annotations

import json
import logging
import subprocess
import urllib.request
from datetime import datetime

from lighthouse.observations.types import Observation

log = logging.getLogger(__name__)

CDP_URL = "http://localhost:9222/json"

# Fallback AppleScript
_ALL_TABS_SCRIPT = '''
tell application "Google Chrome"
    set tabList to {}
    repeat with w in every window
        repeat with t in every tab of w
            set end of tabList to (title of t) & " ||| " & (URL of t)
        end repeat
    end repeat
    set AppleScript's text item delimiters to "\\n"
    return tabList as text
end tell
'''

_ACTIVE_TAB_SCRIPT = '''
tell application "Google Chrome"
    return (URL of active tab of front window)
end tell
'''


def _collect_via_cdp() -> list[dict] | None:
    """Try Chrome DevTools Protocol to get all tabs across all profiles."""
    try:
        req = urllib.request.Request(CDP_URL, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read())
        tabs = []
        for entry in data:
            if entry.get("type") == "page":
                tabs.append({
                    "title": entry.get("title", ""),
                    "url": entry.get("url", ""),
                })
        return tabs if tabs else None
    except Exception:
        return None


def _get_active_url() -> str:
    """Get the active tab URL via AppleScript."""
    try:
        r = subprocess.run(
            ["osascript", "-e", _ACTIVE_TAB_SCRIPT],
            capture_output=True, text=True, timeout=5,
        )
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def _collect_via_applescript() -> list[dict]:
    """Fallback: get tabs via AppleScript."""
    try:
        r = subprocess.run(
            ["osascript", "-e", _ALL_TABS_SCRIPT],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0 or not r.stdout.strip():
            return []
        tabs = []
        for line in r.stdout.strip().split("\n"):
            parts = line.split(" ||| ", 1)
            title = parts[0].strip()
            url = parts[1].strip() if len(parts) > 1 else ""
            tabs.append({"title": title, "url": url})
        return tabs
    except Exception:
        return []


def collect_chrome_tabs() -> list[Observation]:
    """Get open Chrome tab titles and URLs, with active tab marked."""
    # Try CDP first (gets all profiles), fall back to AppleScript
    tabs = _collect_via_cdp()
    if tabs is None:
        tabs = _collect_via_applescript()

    if not tabs:
        return []

    active_url = _get_active_url()
    now = datetime.now()
    results: list[Observation] = []

    for tab in tabs:
        title = tab["title"]
        url = tab["url"]
        id_key = url or title
        is_active = url == active_url and active_url != ""
        label = f"[ACTIVE] {title}" if is_active else title
        results.append(
            Observation(
                source="chrome",
                sender=url,
                text=label[:500],
                timestamp=now,
                id_key=id_key,
            )
        )

    return results
