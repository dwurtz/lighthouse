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


# Timestamp of the most recent voice entry we already injected into a
# vision prompt. Subsequent calls refuse to re-return the same entry,
# so only the FIRST screenshot after a dictation gets grounded on the
# user's words — the next background screenshot is treated as unrelated
# even if it lands inside the lookback window.
_last_consumed_voice_ts: float = 0.0


def _recent_voice_context(window_seconds: int = 15) -> str:
    """Find a voice dictation that happened within the last N seconds and
    hasn't already been injected into a previous vision prompt.

    Used to ground vision descriptions in the user's spoken intent when
    they just dictated something. Critical that this returns empty when
    no fresh dictation exists — the vision prompt has an ``if voice_context:``
    gate and we never want to inject a stale "# IMPORTANT: the user just
    said this" block into unrelated background screenshots.

    Returns empty string when:
      - No observations.jsonl file yet
      - No ``[spoken]`` entries in the recent tail at all
      - The most recent ``[spoken]`` entry is older than window_seconds
      - The most recent ``[spoken]`` entry has already been consumed by
        a prior call (mark-as-consumed via ``_last_consumed_voice_ts``)

    The mark-as-consumed guard is what distinguishes "the user just spoke"
    from "the user spoke 10 seconds ago and this is a different screenshot."
    Without it, every screenshot inside the 15-second window would re-inject
    the same voice prompt, bleeding the user's dictation into captures
    taken while they've moved on to something else.
    """
    global _last_consumed_voice_ts

    import json
    from pathlib import Path
    from deja.config import DEJA_HOME

    obs_log = DEJA_HOME / "observations.jsonl"
    if not obs_log.exists():
        return ""

    cutoff = datetime.now(timezone.utc).timestamp() - window_seconds

    try:
        # Read the last ~16 KB of observations (plenty for the window)
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
            if ts < cutoff:
                # Older than the window — older entries are even older.
                return ""
            if ts <= _last_consumed_voice_ts:
                # Already injected into a previous vision prompt; don't
                # bleed the user's old words into an unrelated capture.
                return ""
            _last_consumed_voice_ts = ts
            return text[len("[spoken] "):]
    except Exception as e:
        log.debug("recent voice context lookup failed: %s", e)

    return ""


async def _shadow_save_triple(
    loop_ref,
    image_path: str,
    local_desc: str,
    voice_context: str,
    timestamp: datetime,
) -> None:
    """Run cloud vision on the same image and save (image, local, cloud) triple.

    Used by VISION_SHADOW_EVAL mode to build a real-world dataset for
    iterating on the local vision model's prompt.
    """
    import json
    import shutil
    from deja.config import VISION_SHADOW_DIR

    VISION_SHADOW_DIR.mkdir(parents=True, exist_ok=True)
    ts = timestamp.strftime("%Y%m%d-%H%M%S")

    # Run cloud vision (Gemini) on the same image
    cloud_desc = ""
    cloud_error = ""
    try:
        cloud_result = await loop_ref.gemini.describe_screen(image_path, voice_context=voice_context)
        cloud_desc = (cloud_result.get("summary") or "").strip()
    except Exception as e:
        cloud_error = str(e)[:200]
        log.warning("vision shadow: cloud call failed: %s", cloud_error)
        return

    if not cloud_desc:
        return

    # Save the triple: image + JSON metadata
    try:
        dest_png = VISION_SHADOW_DIR / f"{ts}.png"
        dest_meta = VISION_SHADOW_DIR / f"{ts}.json"
        shutil.copy(image_path, dest_png)
        meta = {
            "timestamp": timestamp.isoformat(),
            "voice_context": voice_context,
            "local_desc": local_desc,
            "cloud_desc": cloud_desc,
        }
        dest_meta.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        log.info("vision shadow: saved %s (local=%dch cloud=%dch)",
                 ts, len(local_desc), len(cloud_desc))
    except Exception as e:
        log.warning("vision shadow: failed to write triple: %s", e)


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

                        # Vision shadow eval — if both models ran, save the
                        # paired (image, local, cloud) triple for prompt iteration.
                        # The downstream `sig.text` is unchanged; we only collect.
                        try:
                            from deja.config import VISION_SHADOW_EVAL
                            if VISION_SHADOW_EVAL and local_desc:
                                await _shadow_save_triple(
                                    loop_ref, image_path, local_desc,
                                    voice_context, sig.timestamp,
                                )
                        except Exception:
                            log.debug("vision shadow save failed", exc_info=True)
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
