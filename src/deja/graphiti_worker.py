"""Graphiti ingest worker — subprocess entry point.

Tails ``~/.deja/graphiti_queue.jsonl`` and runs
``graphiti_ingest.ingest_signal`` for each new line. Keeps a byte-offset
cursor in ``~/.deja/graphiti_queue.cursor`` so that a crash / SIGTERM
restart does not re-process episodes already committed.

Design notes:
    - Single process, single event loop. No threads, no pools.
    - Sequential: one ``add_episode`` at a time. This is intentional —
      Kuzu's AsyncConnection deadlocked when we ran concurrent writes.
    - Lazy graphiti init: the first signal triggers ``_ensure_graphiti``
      (inside ``ingest_signal``). Worker startup is instant.
    - Poll-based tailing: checks the queue file every 500ms for new
      bytes past the cursor. Not inotify / kqueue because the polling
      overhead is negligible and it stays portable.
    - Cursor semantics: the cursor stores "the byte offset of the next
      unread line". We only advance past a line AFTER ``ingest_signal``
      returns (success or logged failure — we do not retry forever).
    - Malformed JSON: logged and skipped (cursor advances).
    - Queue file missing: wait for it (don't crash).
    - SIGTERM: set a flag; finish the current episode, save cursor, exit.

Run from the Swift BackendProcessManager alongside ``deja monitor`` and
``deja web``. Parent logs are captured via the standard Python logging
config in ``deja.__main__._setup_logging`` (written to
``~/.deja/deja.log``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal as _signal
import sys
from pathlib import Path

from deja.config import DEJA_HOME
from deja.graphiti_ingest import QUEUE_PATH, ingest_signal

log = logging.getLogger("deja.graphiti_worker")

CURSOR_PATH = DEJA_HOME / "graphiti_queue.cursor"
POLL_INTERVAL_SEC = 0.5

# Set by the SIGTERM handler. The main loop checks this between episodes
# so we finish the one in flight rather than losing it.
_shutdown = False


def _read_cursor() -> int:
    """Return the byte offset the worker should start reading from."""
    try:
        if CURSOR_PATH.exists():
            raw = CURSOR_PATH.read_text().strip()
            if raw:
                return max(0, int(raw))
    except (ValueError, OSError):
        log.warning("graphiti_worker: cursor unreadable, starting from 0",
                    exc_info=True)
    return 0


def _write_cursor(offset: int) -> None:
    """Atomically persist the cursor (write to .tmp, rename)."""
    try:
        CURSOR_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CURSOR_PATH.with_suffix(".cursor.tmp")
        tmp.write_text(str(offset))
        os.replace(tmp, CURSOR_PATH)
    except OSError:
        log.warning("graphiti_worker: cursor write failed", exc_info=True)


def _clamp_cursor_to_file(offset: int) -> int:
    """If the queue file was truncated / deleted, don't read past its end."""
    try:
        size = QUEUE_PATH.stat().st_size
        if offset > size:
            # File shrank or was rotated. Restart from 0 — everything in
            # the new file is unprocessed.
            log.info("graphiti_worker: queue file shrank (%d > %d), "
                     "resetting cursor", offset, size)
            return 0
    except FileNotFoundError:
        return 0
    return offset


async def _process_one_line(raw_line: str) -> None:
    """Parse + ingest one queue line. Never raises."""
    try:
        signal = json.loads(raw_line)
    except json.JSONDecodeError:
        log.warning("graphiti_worker: malformed JSON, skipping: %r",
                    raw_line[:200])
        return
    if not isinstance(signal, dict):
        log.warning("graphiti_worker: non-dict record, skipping: %r",
                    raw_line[:200])
        return
    try:
        await ingest_signal(signal)
    except Exception:
        # ingest_signal itself catches + logs, but guard anyway so one
        # bad line can't crash the worker.
        log.warning("graphiti_worker: ingest_signal raised", exc_info=True)


async def _drain_once(start_offset: int) -> int:
    """Read new complete lines from the queue file starting at offset.

    Returns the new cursor position. A trailing partial line (writer
    mid-append) is left un-consumed: cursor stops at the last '\\n'.
    """
    if not QUEUE_PATH.exists():
        return start_offset

    try:
        with open(QUEUE_PATH, "rb") as f:
            f.seek(start_offset)
            chunk = f.read()
    except OSError:
        log.warning("graphiti_worker: queue read failed", exc_info=True)
        return start_offset

    if not chunk:
        return start_offset

    # Split on newline; keep the (possibly partial) tail after the last \n
    # un-consumed so we don't half-process a line the producer is still
    # writing.
    last_nl = chunk.rfind(b"\n")
    if last_nl < 0:
        return start_offset  # No complete line yet

    complete = chunk[: last_nl + 1]
    # Decode and split into lines
    try:
        text = complete.decode("utf-8")
    except UnicodeDecodeError:
        text = complete.decode("utf-8", errors="replace")
        log.warning("graphiti_worker: non-utf8 bytes in queue (replaced)")

    offset = start_offset
    for line in text.splitlines():
        if _shutdown:
            break
        line_bytes = (line + "\n").encode("utf-8")
        # Length in the original file — re-encoding is the right metric
        # because we read bytes from disk, not characters.
        await _process_one_line(line)
        offset += len(line_bytes)
        _write_cursor(offset)

    return offset


async def main() -> None:
    """Worker main loop. Runs until SIGTERM or the event loop is cancelled."""
    log.info("graphiti_worker: starting (queue=%s, cursor=%s)",
             QUEUE_PATH, CURSOR_PATH)

    # Install signal handlers on the running loop so SIGTERM triggers a
    # clean shutdown instead of KeyboardInterrupt-style traceback.
    loop = asyncio.get_running_loop()

    def _on_signal(signame: str) -> None:
        global _shutdown
        log.info("graphiti_worker: received %s, shutting down", signame)
        _shutdown = True

    for sig in (_signal.SIGTERM, _signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _on_signal, sig.name)
        except (NotImplementedError, ValueError):
            # Windows / restricted contexts — best effort.
            pass

    cursor = _read_cursor()
    log.info("graphiti_worker: starting at byte offset %d", cursor)

    while not _shutdown:
        cursor = _clamp_cursor_to_file(cursor)
        new_cursor = await _drain_once(cursor)
        if new_cursor != cursor:
            cursor = new_cursor
            # Immediate re-drain in case more lines arrived while we were
            # ingesting — keeps latency low when the queue is busy.
            continue
        # Idle: nothing new. Sleep briefly, but stay responsive to signals.
        try:
            await asyncio.sleep(POLL_INTERVAL_SEC)
        except asyncio.CancelledError:
            break

    # Final cursor flush on shutdown — our inner loop already writes
    # after every episode, but one extra write is cheap insurance.
    _write_cursor(cursor)
    log.info("graphiti_worker: stopped at offset %d", cursor)


def _setup_logging() -> None:
    """Mirror ``deja.__main__._setup_logging`` so logs land in deja.log."""
    DEJA_HOME.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(DEJA_HOME / "deja.log"),
            logging.StreamHandler(),
        ],
    )


if __name__ == "__main__":
    _setup_logging()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("graphiti_worker: interrupted")
        sys.exit(0)
