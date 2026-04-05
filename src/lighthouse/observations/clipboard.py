"""Collect clipboard contents."""

from __future__ import annotations

import hashlib
import logging
import subprocess
from datetime import datetime

from lighthouse.observations.types import Observation

log = logging.getLogger(__name__)


def collect_clipboard() -> Signal | None:
    """Return clipboard contents as a Signal, or None if empty."""
    try:
        r = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=5)
        text = r.stdout.strip()[:500]
        if not text:
            return None
        h = hashlib.md5(text.encode()).hexdigest()[:16]
        return Observation(
            source="clipboard",
            sender="clipboard",
            text=text,
            timestamp=datetime.now(),
            id_key=f"clip-{h}",
        )
    except Exception:
        log.exception("Clipboard collection failed")
        return None
