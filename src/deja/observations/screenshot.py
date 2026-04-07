"""Screenshot capture — vision-only, no OCR.

Captures the frontmost window with `screencapture`, deduplicates by
perceptual hash so identical screens don't trigger repeat vision calls,
and returns a signal with the image path attached. The monitor loop then
sends the image to the vision model, which describes the active screen
in plain prose. No OCR, no text extraction, no keyword matching — the
vision model is trusted to describe what matters.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
import time
from datetime import datetime

from deja.observations.types import Observation

log = logging.getLogger(__name__)

_last_image_hash = None

# Screen Recording permission state machine.
# - None: not yet tested
# - True: granted, capture every cycle
# - False: denied, retry every _RETRY_INTERVAL seconds
_permission_granted: bool | None = None
_last_denied_time: float = 0
_RETRY_INTERVAL = 60.0  # re-check every 60s when denied (not every 3s)
_notified: bool = False


def screen_recording_granted() -> bool:
    """Public API: whether Screen Recording is currently working.
    Used by the status endpoint to surface warnings in the popover.
    """
    return _permission_granted is True


def capture_screenshot_if_changed() -> Observation | None:
    """Capture the screen, dedup by perceptual hash, return an Observation.

    The returned observation has ``_image_path`` attached so the agent
    loop can send the image to the vision model. Returns None if capture
    fails or the screen hasn't meaningfully changed since the last capture.

    Permission handling: if screencapture produces an empty/tiny file
    (Screen Recording denied), backs off to 60-second retries and sends
    a one-time macOS notification so the user knows to grant access.
    """
    global _last_image_hash, _permission_granted, _last_denied_time, _notified

    # If previously denied, only retry every 60 seconds
    if _permission_granted is False:
        now = time.monotonic()
        if (now - _last_denied_time) < _RETRY_INTERVAL:
            return None

    path = tempfile.mktemp(suffix=".png")
    try:
        result = subprocess.run(
            ["screencapture", "-x", "-C", path],
            capture_output=True,
            timeout=5,
        )
    except subprocess.TimeoutExpired:
        log.warning("screencapture timed out")
        if os.path.exists(path):
            os.remove(path)
        return None
    except FileNotFoundError:
        log.warning("screencapture not found")
        return None

    # Check if capture succeeded — denied permission produces an empty
    # or very small file (< 1KB is not a real screenshot)
    if not os.path.exists(path):
        _mark_denied()
        return None

    file_size = os.path.getsize(path)
    if file_size < 1024:
        os.remove(path)
        _mark_denied()
        return None

    # Capture succeeded — permission is granted
    if _permission_granted is not True:
        _permission_granted = True
        log.info("Screen Recording permission confirmed — screenshots active")

    # Perceptual hash dedup — skip identical or near-identical frames.
    try:
        import imagehash
        from PIL import Image

        current_hash = imagehash.phash(Image.open(path))
        if _last_image_hash is not None and (current_hash - _last_image_hash) < 8:
            os.remove(path)
            return None
        _last_image_hash = current_hash
    except Exception:
        log.exception("imagehash dedup failed — proceeding without dedup")

    sig = Observation(
        source="screenshot",
        sender="screen",
        text="(pending vision description)",
        timestamp=datetime.now(),
        id_key=f"screen-{datetime.now().strftime('%H%M%S')}",
    )
    sig._image_path = path
    return sig


def _mark_denied() -> None:
    """Record that Screen Recording was denied and notify the user once."""
    global _permission_granted, _last_denied_time, _notified

    _permission_granted = False
    _last_denied_time = time.monotonic()

    if not _notified:
        _notified = True
        log.warning(
            "Screen Recording permission not granted — screenshots disabled. "
            "Grant in System Settings → Privacy & Security → Screen Recording."
        )
        try:
            subprocess.run(
                [
                    "osascript", "-e",
                    'display notification "Grant Screen Recording access in '
                    'System Settings → Privacy & Security to enable screenshots." '
                    'with title "Déjà" sound name "Basso"',
                ],
                capture_output=True,
                timeout=5,
            )
        except Exception:
            pass
