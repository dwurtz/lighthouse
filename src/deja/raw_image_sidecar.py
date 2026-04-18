"""Raw screenshot sidecar — preserve the PNG per screenshot observation.

Mirror of raw_ocr_sidecar, for the image itself rather than its text.
The observation pipeline currently deletes each screen_*.png after OCR
runs, so by the time integrate fires there's no way to get the image
back. The sidecar copies the PNG to
``~/.deja/raw_images/<YYYY-MM-DD>/<id_key>.png`` before the pipeline
deletes the original, enabling a Claude vision shadow that reasons on
the actual pixels rather than OCR text.

Partitioned by date so cleanup is a directory-rm per day.
"""
from __future__ import annotations

import logging
import shutil
from datetime import datetime
from pathlib import Path

from deja.config import DEJA_HOME

log = logging.getLogger(__name__)

SIDECAR_ROOT = DEJA_HOME / "raw_images"


def _path(id_key: str, day: str | None = None) -> Path:
    d = day or datetime.now().strftime("%Y-%m-%d")
    return SIDECAR_ROOT / d / f"{id_key}.png"


def write(id_key: str, image_path: str | Path) -> None:
    """Copy the live screenshot PNG to the sidecar. Best-effort."""
    if not id_key or not image_path:
        return
    src = Path(image_path)
    if not src.exists():
        return
    dst = _path(id_key)
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, dst)
    except OSError as e:
        log.debug("raw_image sidecar write failed for %s: %s", id_key, e)


def read_bytes(id_key: str, day: str | None = None) -> bytes | None:
    """Return the image bytes, or None if the sidecar doesn't exist."""
    if not id_key:
        return None
    p = _path(id_key, day)
    if not p.exists():
        return None
    try:
        return p.read_bytes()
    except OSError:
        return None


def path(id_key: str, day: str | None = None) -> Path | None:
    """Return the sidecar path if it exists, else None."""
    if not id_key:
        return None
    p = _path(id_key, day)
    return p if p.exists() else None


__all__ = ["SIDECAR_ROOT", "write", "read_bytes", "path"]
