"""Screenshot observer — reads frames captured by the Swift app.

The Swift menubar app (Deja.swift) captures the screen every 6 seconds
and writes the image to ~/.deja/latest_screen.png with a timestamp in
~/.deja/latest_screen_ts.txt. This module reads that file, deduplicates
by perceptual hash, and returns an Observation with the image path.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import time
from datetime import datetime

from deja.observations.base import BaseObserver
from deja.observations.types import Observation

log = logging.getLogger(__name__)

_last_image_hash = None
_last_read_ts: float = 0

_DEJA_HOME = os.path.expanduser("~/.deja")
_SCREENSHOT_PATH = os.path.join(_DEJA_HOME, "latest_screen.png")
_TIMESTAMP_PATH = os.path.join(_DEJA_HOME, "latest_screen_ts.txt")


class ScreenshotObserver(BaseObserver):
    """Captures screenshots when the visual state changes, with perceptual dedup."""

    @property
    def name(self) -> str:
        return "Screenshot"

    def collect(self) -> list[Observation]:
        result = capture_screenshot_if_changed()
        return [result] if result else []


def screen_recording_granted() -> bool:
    """Return True if the Swift app updated the screenshot recently (within 30s)."""
    try:
        ts_str = open(_TIMESTAMP_PATH).read().strip()
        ts = float(ts_str)
        return (time.time() - ts) < 30.0
    except Exception:
        return False


def capture_screenshot_if_changed() -> Observation | None:
    """Read the latest screenshot from disk, dedup by perceptual hash."""
    global _last_image_hash, _last_read_ts

    # Check timestamp file to see if there's a new frame
    try:
        ts_str = open(_TIMESTAMP_PATH).read().strip()
        ts = float(ts_str)
    except Exception:
        return None

    # Skip if we already processed this frame
    if ts <= _last_read_ts:
        return None

    # Verify the screenshot file exists and has content
    if not os.path.exists(_SCREENSHOT_PATH) or os.path.getsize(_SCREENSHOT_PATH) < 1024:
        return None

    _last_read_ts = ts

    # Copy to a temp file so the Swift app can overwrite the original freely
    path = tempfile.mktemp(suffix=".png")
    try:
        shutil.copy2(_SCREENSHOT_PATH, path)
    except Exception:
        log.debug("Failed to copy screenshot file", exc_info=True)
        return None

    # Perceptual hash dedup — skip identical or near-identical frames
    try:
        import imagehash
        from PIL import Image

        current_hash = imagehash.phash(Image.open(path))
        if _last_image_hash is not None and (current_hash - _last_image_hash) < 8:
            os.remove(path)
            return None
        _last_image_hash = current_hash
    except Exception:
        log.debug("imagehash dedup failed — proceeding without dedup", exc_info=True)

    sig = Observation(
        source="screenshot",
        sender="screen",
        text="(pending vision description)",
        timestamp=datetime.now(),
        id_key=f"screen-{datetime.now().strftime('%H%M%S')}",
    )
    sig._image_path = path
    return sig
