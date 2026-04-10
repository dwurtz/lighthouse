"""Observation (signal collection) cycle — extracted from AgentLoop.

Handles one collection cycle: gather signals from all sources, run
vision on screenshots, persist, and update stats.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

log = logging.getLogger(__name__)


def _recent_voice_context(window_seconds: int = 30) -> str:
    """Find any voice dictation that happened within the last N seconds.

    Returns the most recent voice transcript, or empty string if none.
    Used to ground vision descriptions in the user's spoken intent.
    """
    import json
    from pathlib import Path
    from deja.config import DEJA_HOME

    obs_log = DEJA_HOME / "observations.jsonl"
    if not obs_log.exists():
        return ""

    cutoff = datetime.now(timezone.utc).timestamp() - window_seconds

    try:
        # Read the last ~50 lines (recent observations)
        with open(obs_log, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            chunk = min(size, 16384)
            f.seek(size - chunk)
            tail = f.read().decode("utf-8", errors="replace")

        for line in reversed(tail.splitlines()):
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            text = obj.get("text", "")
            if not text.startswith("[spoken] "):
                continue
            ts_str = obj.get("timestamp", "")
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
            except (ValueError, AttributeError):
                continue
            if ts >= cutoff:
                return text[len("[spoken] "):]
            else:
                # Older than the window — older entries are even older
                break
    except Exception as e:
        log.debug("recent voice context lookup failed: %s", e)

    return ""


async def run_collect_cycle(loop_ref) -> None:
    """One collection cycle -- gather all new signals.

    ``loop_ref`` is the AgentLoop instance (used for gemini client,
    collector, stats counters, and the stats callback).
    """
    loop_ref.phase = "OBSERVING"

    loop = asyncio.get_running_loop()
    new_signals = await loop.run_in_executor(None, loop_ref.collector.collect_all)

    if new_signals:
        for sig in new_signals:
            if sig.source == "screenshot":
                # Screenshots are the only source that needs post-collection
                # processing: the collector hands us a raw PNG path, we run
                # vision on it, replace the placeholder text with the
                # description, optionally retain a copy for vision eval,
                # delete the original, and persist.
                image_path = getattr(sig, "_image_path", None)
                if image_path:
                    try:
                        # Look for recent voice dictation — if the user spoke
                        # within ~30s of this screenshot, treat their words as
                        # primary context for the vision model.
                        voice_context = _recent_voice_context(window_seconds=30)

                        # Try local FastVLM first (private, free, ~3.5s)
                        # Falls back to Gemini via proxy if mlx-vlm not installed
                        from deja.vision_local import describe_screen_local, is_available
                        local_desc = None
                        avail = is_available()
                        log.info("Local vision available: %s", avail)
                        if avail:
                            local_desc = describe_screen_local(image_path, voice_context=voice_context)
                            log.info("Local vision result: %s", "OK" if local_desc else "EMPTY")

                        if local_desc:
                            sig.text = local_desc
                        else:
                            # Fallback to cloud Gemini
                            vision_result = await loop_ref.gemini.describe_screen(image_path, voice_context=voice_context)
                            sig.text = (vision_result.get("summary") or "").strip() or "(empty vision description)"
                    finally:
                        # Optional retention for vision A/B eval — gated by
                        # config.VISION_RETENTION. Saves PNG + sidecar .txt
                        # so we can rerun alternate models against real frames.
                        try:
                            from deja.config import VISION_RETENTION, VISION_RETENTION_DIR
                            if VISION_RETENTION:
                                import shutil
                                VISION_RETENTION_DIR.mkdir(parents=True, exist_ok=True)
                                ts = sig.timestamp.strftime("%Y%m%d-%H%M%S")
                                dest_png = VISION_RETENTION_DIR / f"{ts}.png"
                                dest_txt = VISION_RETENTION_DIR / f"{ts}.txt"
                                shutil.copy(image_path, dest_png)
                                dest_txt.write_text(sig.text, encoding="utf-8")
                        except Exception:
                            log.debug("vision retention failed", exc_info=True)
                        try:
                            os.remove(image_path)
                        except OSError:
                            pass
                loop_ref.collector._persist_signal(sig)

        loop_ref.signals_collected += len(new_signals)
        loop_ref.last_signal_time = datetime.now(timezone.utc)
        log.info("Collected %d new signals", len(new_signals))
        loop_ref._fire_stats_update()

    loop_ref.phase = "IDLE"
