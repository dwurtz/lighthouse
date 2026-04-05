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
from datetime import datetime

from lighthouse.observations.types import Observation

log = logging.getLogger(__name__)

_last_image_hash = None


def capture_screenshot_if_changed() -> Observation | None:
    """Capture the screen, dedup by perceptual hash, return an Observation.

    The returned observation has ``_image_path`` attached so the agent
    loop can send the image to the vision model. Returns None if capture
    fails or the screen hasn't meaningfully changed since the last capture.
    """
    global _last_image_hash

    path = tempfile.mktemp(suffix=".png")
    try:
        subprocess.run(
            ["screencapture", "-x", "-C", path],
            capture_output=True,
            timeout=5,
        )
    except subprocess.TimeoutExpired:
        log.warning("screencapture timed out")
        if os.path.exists(path):
            os.remove(path)
        return None

    if not os.path.exists(path):
        return None

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
