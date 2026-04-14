"""Ingest typed-content snapshots written by the Swift menubar app.

The macOS frontend runs a ``TypedContentMonitor`` (see
``menubar/Sources/Services/TypedContentMonitor.swift``) that snapshots
the current focused text field via the Accessibility API whenever the
user was recently typing, and appends one JSONL record per "finished
thought" to ``~/.deja/typed_content.jsonl``:

    {
      "timestamp": "2026-04-13T14:32:00Z",
      "app": "Mail",
      "window_title": "Re: theme feedback",
      "element_role": "AXTextArea",
      "text": "<content>",
      "char_count": 142
    }

This observer tails that file (byte-offset persisted in memory) and
converts each row into an ``Observation`` with ``source="typed"`` and
``sender="you"`` — typed text is, by definition, outbound user signal.
The standard collector pipeline then persists it to ``observations.jsonl``
with the rest of the observations.

Privacy notes live in the Swift module's docstring; this file is a
pure ingest shim.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path

from deja.config import DEJA_HOME
from deja.observations.base import BaseObserver
from deja.observations.types import Observation

log = logging.getLogger(__name__)

TYPED_CONTENT_LOG = Path(DEJA_HOME) / "typed_content.jsonl"


class TypedContentObserver(BaseObserver):
    """Reads new rows from ~/.deja/typed_content.jsonl since last poll."""

    def __init__(self) -> None:
        self._offset: int = 0
        # On startup, skip everything already in the file — we only
        # want events from this session onward. Restarting the monitor
        # shouldn't re-ingest the user's historical typing.
        if TYPED_CONTENT_LOG.exists():
            try:
                self._offset = TYPED_CONTENT_LOG.stat().st_size
            except OSError:
                self._offset = 0

    @property
    def name(self) -> str:
        return "TypedContent"

    def collect(self) -> list[Observation]:
        if not TYPED_CONTENT_LOG.exists():
            return []

        try:
            size = TYPED_CONTENT_LOG.stat().st_size
        except OSError:
            return []

        # File shrank (rotated / truncated) — reset.
        if size < self._offset:
            self._offset = 0

        if size == self._offset:
            return []

        out: list[Observation] = []
        try:
            with open(TYPED_CONTENT_LOG, "rb") as f:
                f.seek(self._offset)
                chunk = f.read()
                self._offset = f.tell()
        except OSError:
            log.exception("Failed to read typed_content.jsonl")
            return []

        for raw in chunk.splitlines():
            if not raw.strip():
                continue
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                continue
            obs = _row_to_observation(row)
            if obs is not None:
                out.append(obs)
        return out


def _row_to_observation(row: dict) -> Observation | None:
    text = (row.get("text") or "").strip()
    if not text:
        return None

    ts_str = row.get("timestamp") or ""
    try:
        # Swift writes "...Z" Internet date-time. fromisoformat handles
        # the Z suffix on 3.11+; be defensive for older runtimes.
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except ValueError:
        ts = datetime.now()

    h = hashlib.sha1(
        f"{text}|{ts_str}".encode("utf-8", errors="ignore")
    ).hexdigest()[:12]

    # Prepend app/window context to the text so downstream stages have
    # it without needing a new schema field (spec pins sender="you").
    app = row.get("app") or "Unknown"
    window_title = row.get("window_title") or ""
    header = f"[{app}"
    if window_title:
        header += f" — {window_title[:80]}"
    header += "]"
    body = f"{header} {text}"

    return Observation(
        source="typed",
        sender="you",
        text=body,
        timestamp=ts,
        id_key=f"typed-{h}",
    )
