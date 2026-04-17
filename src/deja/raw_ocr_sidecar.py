"""Raw OCR sidecar — preserve the unadulterated Apple Vision text per screenshot.

The agent pipeline hands raw OCR to ``screenshot_preprocess`` which runs
a VLM (currently Gemini Flash-Lite) to produce a structured TYPE/WHAT/
SALIENT_FACTS block. That structured output replaces the raw text in
the observation log. Problem: the VLM hallucinates structured
extractions on dense views (e.g. inventing APPOINTMENT times from
calendar cells it misreads).

The sidecar preserves the raw OCR so a downstream consumer (the
Claude integrate shadow, for one) can reconstruct what the agent
pipeline actually saw before the VLM layer had a chance to poison it.

Layout
------

``~/.deja/raw_ocr/<YYYY-MM-DD>/<id_key>.txt``

Partitioned by date so we can drop old days without scanning. No
metadata files — the observation log already carries the id_key,
source, timestamp, app, window title alongside the preprocessed text.
One file per screenshot; small; optional to read.

Contract
--------

``write(id_key, ocr_text)`` — best-effort; never raises. Called at
write time of each screenshot observation, before preprocess.

``read(id_key, day=None)`` — returns ``None`` if the sidecar doesn't
exist, empty string if written but empty, otherwise the raw OCR text.
``day`` defaults to today but callers can pass a YYYY-MM-DD for
historical reads.
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from deja.config import DEJA_HOME

log = logging.getLogger(__name__)

SIDECAR_ROOT = DEJA_HOME / "raw_ocr"


def _path(id_key: str, day: str | None = None) -> Path:
    d = day or datetime.now().strftime("%Y-%m-%d")
    return SIDECAR_ROOT / d / f"{id_key}.txt"


def write(id_key: str, ocr_text: str) -> None:
    """Persist raw OCR text as a sidecar. Best-effort — never raises."""
    if not id_key or ocr_text is None:
        return
    p = _path(id_key)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(ocr_text, encoding="utf-8")
    except OSError as e:
        log.debug("raw_ocr sidecar write failed for %s: %s", id_key, e)


def read(id_key: str, day: str | None = None) -> str | None:
    """Return the raw OCR text for ``id_key``, or None if no sidecar exists."""
    if not id_key:
        return None
    p = _path(id_key, day)
    if not p.exists():
        return None
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return None


__all__ = ["SIDECAR_ROOT", "write", "read"]
