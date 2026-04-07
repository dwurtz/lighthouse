"""Get the frontmost application name and window title."""

from __future__ import annotations

import logging
import subprocess

log = logging.getLogger(__name__)

_WINDOW_TITLE_SCRIPT = '''
tell application "System Events"
    set fp to first process whose frontmost is true
    if (count of windows of fp) > 0 then
        return name of front window of fp
    end if
end tell
return ""
'''


def get_active_app() -> tuple[str, str]:
    """
    Return (app_name, window_title) for the frontmost application.

    Returns ("Unknown", "") on failure.
    """
    app_name = "Unknown"
    window_title = ""

    try:
        r = subprocess.run(
            [
                "osascript",
                "-e",
                'tell application "System Events" to get name of first process whose frontmost is true',
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip():
            app_name = r.stdout.strip()
    except Exception:
        log.exception("Failed to get active app name")

    try:
        r = subprocess.run(
            ["osascript", "-e", _WINDOW_TITLE_SCRIPT],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip():
            window_title = r.stdout.strip()
    except Exception:
        log.exception("Failed to get window title")

    return app_name, window_title
