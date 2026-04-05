"""Browser history signal collector.

Reads recent URL visits from the *active* profile of each installed
Chromium browser (Chrome, Arc, Brave, Edge). Firefox isn't handled — its
schema is different.

Active-profile resolution: every Chromium browser writes a `Local State`
file at its root that records `profile.last_used` (the most recently
activated profile in the current session). We parse that and only read
from the matching profile. If the file is missing or malformed, we fall
back to `Default`. A browser with no recent activity in the last cycle
window produces zero signals and is effectively invisible.

Each browser holds an exclusive lock on the live `History` SQLite file,
so we always read via a tempfile copy. Timestamps are Chrome epoch
(microseconds since 1601-01-01 UTC) and get converted to local datetimes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from lighthouse.observations.types import Observation

log = logging.getLogger(__name__)

# Seconds between 1601-01-01 (Chrome epoch) and 1970-01-01 (Unix epoch)
_CHROME_EPOCH_OFFSET = 11644473600

# Chromium-based browsers. (display_name, root_dir)
_BROWSER_ROOTS: list[tuple[str, Path]] = [
    ("Chrome", Path.home() / "Library/Application Support/Google/Chrome"),
    ("Arc", Path.home() / "Library/Application Support/Arc/User Data"),
    ("Brave", Path.home() / "Library/Application Support/BraveSoftware/Brave-Browser"),
    ("Edge", Path.home() / "Library/Application Support/Microsoft Edge"),
    ("Chromium", Path.home() / "Library/Application Support/Chromium"),
]

# URL prefixes we drop unconditionally — these are browser/extension chrome,
# not real pages. NOT a significance filter; the analysis cycle still judges
# what matters among actual URLs.
_SKIP_URL_PREFIXES = (
    "chrome://",
    "chrome-extension://",
    "brave://",
    "edge://",
    "arc://",
    "about:",
    "file://",
    "view-source:",
    "devtools://",
    "javascript:",
)


def _active_profile(root: Path) -> str:
    """Return the directory name of the most recently active profile for a
    Chromium browser installation. Falls back to ``"Default"`` if the
    ``Local State`` file is missing or doesn't specify one.
    """
    local_state = root / "Local State"
    if not local_state.exists():
        return "Default"
    try:
        state = json.loads(local_state.read_text())
    except (json.JSONDecodeError, OSError) as e:
        log.debug("browser: could not read %s: %s", local_state, e)
        return "Default"
    # Chromium stores it under profile.last_used
    last_used = state.get("profile", {}).get("last_used")
    if isinstance(last_used, str) and last_used.strip():
        return last_used
    return "Default"


def _active_history_files() -> list[tuple[str, Path]]:
    """Return [(browser_label, history_db_path)] for the single active
    profile of each installed Chromium browser.
    """
    out: list[tuple[str, Path]] = []
    for name, root in _BROWSER_ROOTS:
        if not root.exists():
            continue
        profile = _active_profile(root)
        hist = root / profile / "History"
        if hist.exists():
            label = name if profile == "Default" else f"{name} ({profile})"
            out.append((label, hist))
    return out


def collect_browser_history(since_minutes: int = 15, limit_per_browser: int = 50) -> list[Observation]:
    """Read URL visits from the last N minutes across the active profile of
    each installed Chromium browser.

    Each visit becomes one ``Signal`` with ``source="browser"``, sender set
    to the browser name, text formatted as ``"<title> — <url>"``, and a
    stable ``id_key`` keyed on URL + visit timestamp so repeated collection
    cycles are idempotent.
    """
    signals: list[Observation] = []

    cutoff_unix = (datetime.now() - timedelta(minutes=since_minutes)).timestamp()
    cutoff_chrome = int((cutoff_unix + _CHROME_EPOCH_OFFSET) * 1_000_000)

    for label, hist_path in _active_history_files():
        tmp_path = tempfile.mktemp(suffix=".db")
        try:
            shutil.copy2(hist_path, tmp_path)
        except OSError as e:
            log.debug("browser: could not copy %s: %s", hist_path, e)
            continue

        try:
            conn = sqlite3.connect(f"file:{tmp_path}?mode=ro", uri=True)
            rows = conn.execute(
                """
                SELECT url, title, last_visit_time
                FROM urls
                WHERE last_visit_time > ?
                ORDER BY last_visit_time DESC
                LIMIT ?
                """,
                (cutoff_chrome, limit_per_browser),
            ).fetchall()
            conn.close()
        except sqlite3.OperationalError as e:
            log.debug("browser: sqlite read failed for %s: %s", hist_path, e)
            rows = []
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass

        for url, title, chrome_ts in rows:
            if not url:
                continue
            if any(url.lower().startswith(p) for p in _SKIP_URL_PREFIXES):
                continue

            try:
                unix_ts = chrome_ts / 1_000_000 - _CHROME_EPOCH_OFFSET
                when = datetime.fromtimestamp(unix_ts)
            except (ValueError, OSError, OverflowError):
                continue

            title_clean = (title or "").strip() or "(no title)"
            text = f"{title_clean} — {url}"

            id_key = "browser-" + hashlib.md5(
                f"{url}-{int(chrome_ts)}".encode()
            ).hexdigest()[:16]

            signals.append(
                Observation(
                    source="browser",
                    sender=label,
                    text=text[:500],
                    timestamp=when,
                    id_key=id_key,
                )
            )

    return signals
