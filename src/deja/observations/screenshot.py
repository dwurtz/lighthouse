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

_last_image_hashes: dict[str, object] = {}  # per-screen perceptual hash
# Per-screen wall-clock time of the last ACCEPTED capture. Used to
# override pHash dedup for stable text-heavy views (terminals, chat
# threads) where content changes substantively but the visual hash
# barely moves — a scrolling cmux session diffs at ~6 bits vs the
# pHash threshold of 8, so every frame gets silently dropped. After
# this many seconds of consecutive dedup-rejects, accept the next
# frame anyway.
_last_accepted_ts: dict[str, float] = {}
_DEDUP_TIME_OVERRIDE_SEC = 90
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
        results = []
        # Process all display screenshots (screen_1.png, screen_2.png, ...)
        import glob
        screen_files = sorted(glob.glob(os.path.join(_DEJA_HOME, "screen_*.png")))
        if screen_files:
            for screen_file in screen_files:
                result = capture_screenshot_if_changed(screenshot_path=screen_file)
                if result:
                    results.append(result)
        else:
            # Fallback to single latest_screen.png
            result = capture_screenshot_if_changed()
            if result:
                results.append(result)
        return results


def screen_recording_granted() -> bool:
    """Return True if the Swift app updated the screenshot recently (within 30s)."""
    try:
        ts_str = open(_TIMESTAMP_PATH).read().strip()
        ts = float(ts_str)
        return (time.time() - ts) < 30.0
    except Exception:
        return False


def capture_screenshot_if_changed(
    screenshot_path: str | None = None,
) -> Observation | None:
    """Read a screenshot from disk, dedup by perceptual hash.

    Args:
        screenshot_path: Path to a specific screen file (e.g. screen_1.png).
                         Falls back to latest_screen.png if not provided.
    """
    global _last_read_ts

    src_path = screenshot_path or _SCREENSHOT_PATH

    # Check timestamp file to see if there's a new frame
    try:
        ts_str = open(_TIMESTAMP_PATH).read().strip()
        ts = float(ts_str)
    except Exception:
        return None

    # Skip if we already processed this frame (only for single-screen mode)
    if screenshot_path is None and ts <= _last_read_ts:
        return None

    # Verify the screenshot file exists and has content
    if not os.path.exists(src_path) or os.path.getsize(src_path) < 1024:
        return None

    if screenshot_path is None:
        _last_read_ts = ts

    # Copy to a temp file so the Swift app can overwrite the original freely
    path = tempfile.mktemp(suffix=".png")
    try:
        shutil.copy2(src_path, path)
    except Exception:
        log.debug("Failed to copy screenshot file", exc_info=True)
        return None

    # Perceptual hash dedup — skip identical or near-identical frames
    hash_key = src_path
    try:
        import imagehash
        from PIL import Image

        current_hash = imagehash.phash(Image.open(path))
        prev_hash = _last_image_hashes.get(hash_key)
        now = time.time()
        if prev_hash is not None:
            distance = current_hash - prev_hash
            last_accepted = _last_accepted_ts.get(hash_key, 0.0)
            time_since = now - last_accepted
            if distance < 8:
                if time_since >= _DEDUP_TIME_OVERRIDE_SEC:
                    log.info(
                        "Screenshot %s: dedup override — distance=%d but %.0fs since last accept",
                        os.path.basename(src_path), distance, time_since,
                    )
                else:
                    log.info(
                        "Screenshot dedup %s: hash distance=%d (threshold=8)",
                        os.path.basename(src_path), distance,
                    )
                    os.remove(path)
                    return None
        else:
            log.info("Screenshot %s: first capture (no prev hash)", os.path.basename(src_path))
        _last_image_hashes[hash_key] = current_hash
        _last_accepted_ts[hash_key] = now
    except Exception:
        log.debug("imagehash dedup failed — proceeding without dedup", exc_info=True)

    # Include display number in the ID so multi-screen observations are distinct
    display_label = os.path.basename(src_path).replace(".png", "").replace("screen_", "display-")
    sig = Observation(
        source="screenshot",
        sender=display_label if screenshot_path else "screen",
        text="(pending vision description)",
        timestamp=datetime.now(),
        id_key=f"{display_label}-{datetime.now().strftime('%H%M%S')}",
    )
    sig._image_path = path
    return sig
