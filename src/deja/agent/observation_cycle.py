"""Observation (signal collection) cycle — extracted from AgentLoop.

Handles one collection cycle: gather signals from all sources, run
vision on screenshots, persist, and update stats.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone

log = logging.getLogger(__name__)

# Screenshots with OCR shorter than this pass through raw — a quick
# iMessage/WhatsApp/toast snapshot is already clean, and the preprocess
# call overhead (~2-4s, one gpt-4.1-mini round-trip) isn't worth paying
# for under ~400 chars of text. Anything above this threshold is where
# UI chrome dominates and preprocessing reliably pays off.
_PREPROCESS_MIN_CHARS = 400

# ---------------------------------------------------------------------------
# OCR signal cleanup — strip menu bar chrome and label signals clearly
# ---------------------------------------------------------------------------


def _read_ax_sidecar(display_sender: str | None) -> dict | None:
    """Read the per-display AX sidecar that Swift writes at capture time.

    ``display_sender`` is the observation's sender field, which the
    screenshot collector sets to ``"display-1"``, ``"display-2"``, etc.
    We convert that to ``screen_N_ax.json`` and read it. This is a
    point-in-time capture — the app/window state at the moment
    screencapture fired — which avoids the "focus moved while Python
    processed the image" race.

    Returns a dict with ``app`` / ``window_title`` keys or None if
    no sidecar exists (e.g. single-screen fallback, fresh install
    before the Swift change rolled out).
    """
    if not display_sender or not display_sender.startswith("display-"):
        return None
    try:
        import json as _json
        import os as _os

        num = display_sender.replace("display-", "")
        path = _os.path.expanduser(f"~/.deja/screen_{num}_ax.json")
        if not _os.path.exists(path):
            return None
        with open(path) as f:
            data = _json.load(f)
        if isinstance(data, dict) and (data.get("app") or data.get("window_title")):
            return data
    except Exception:
        log.debug("ax sidecar read failed", exc_info=True)
    return None


def _label_for_app(app: str, title: str) -> str:
    """Return a signal header like 'Messages: Rob HealthspanMD'.
    Flash-Lite knows what these apps are — no translation needed."""
    if app and title:
        return f"{app}: {title}"
    return app or "Screen content"


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
                        # within ~15s of this screenshot AND we haven't
                        # already injected that entry into a previous vision
                        # call, treat their words as primary context.
                        voice_context = _recent_voice_context()

                        # Accessibility context — frontmost app, window title,
                        # focused element — snapshotted at capture time.
                        # Empty dict when AX is unavailable or the frontmost
                        # app has broken AX support; the formatter handles
                        # that as a no-op in the prompt.
                        # AX context: per-display sidecar written by
                        # Swift at capture time — `screen_N_ax.json`.
                        # This avoids the "wrong app labeled on
                        # screen_1 because focus was on screen_2 when
                        # Python processed it" bug. If no sidecar
                        # exists (single-screen fallback path), fall
                        # back to live AX capture.
                        ax_context = _read_ax_sidecar(sig.sender)
                        if not ax_context:
                            from deja.ax_context import capture as capture_ax
                            ax_context = capture_ax()
                        if ax_context:
                            log.debug(
                                "ax_context: app=%r win=%r focus=%r",
                                ax_context.get("app"),
                                (ax_context.get("window_title") or "")[:60],
                                ax_context.get("focused_role"),
                            )

                        # Vision pipeline: macOS OCR + AX context.
                        #
                        # Apple Vision OCR extracts text perfectly from
                        # any screenshot in ~1.5s on-device. Combined
                        # with AX context (app name + window title), this
                        # gives integrate everything it needs — real text
                        # to reason about, not hallucinated descriptions.
                        #
                        # This replaced FastVLM 3-pass (which scored 90.9%
                        # on fixtures but confabulated message content on
                        # live screens) and cloud Gemini (which was perfect
                        # but cost money and leaked pixels off-device).
                        from deja.vision_local import ocr_screen

                        t_ocr = time.time()
                        ocr_text = ocr_screen(image_path)
                        ocr_elapsed = time.time() - t_ocr

                        if ocr_text:
                            # Label the signal clearly so integrate
                            # knows what kind of content to expect.
                            # The window list is NOT included — it goes
                            # into the integrate prompt once per cycle.
                            app = (ax_context or {}).get("app", "")
                            title = (ax_context or {}).get("window_title", "")
                            label = _label_for_app(app, title)

                            header = f"[{label}]"
                            if voice_context:
                                header = (
                                    f"[User just said: \"{voice_context}\"]\n"
                                    + header
                                )

                            # Preprocess OCR into a compact structured
                            # signal (TYPE / PROJECT / WHAT / SALIENT_FACTS
                            # / CONTENT). Strips UI chrome that would
                            # otherwise dominate integrate's prompt. Only
                            # runs when OCR is long enough to benefit —
                            # short iMessage/WhatsApp snippets pass raw.
                            # Returns None when the screen is pure chrome
                            # (ADMIN_NOISE / MEDIA noise) → drop the
                            # signal entirely.
                            sig_text = ocr_text
                            if len(ocr_text) >= _PREPROCESS_MIN_CHARS:
                                try:
                                    from deja.screenshot_preprocess import (
                                        preprocess_screenshot,
                                    )

                                    condensed = await preprocess_screenshot(
                                        ocr_text,
                                        app_name=app,
                                        window_title=title,
                                    )
                                except Exception:
                                    log.debug(
                                        "preprocess_screenshot failed — using raw OCR",
                                        exc_info=True,
                                    )
                                    condensed = ocr_text
                                if condensed is None:
                                    # SKIP sentinel: pure chrome, nothing
                                    # worth remembering. Drop the signal
                                    # so integrate never sees it.
                                    log.info(
                                        "Preprocess SKIPped screenshot %s (%s — %s)",
                                        image_path.split("/")[-1], app, title,
                                    )
                                    sig.text = ""  # marker: skip persist
                                    continue
                                sig_text = condensed
                                log.info(
                                    "Preprocess: %d → %d chars",
                                    len(ocr_text), len(condensed),
                                )

                            sig.text = header + "\n\n" + sig_text
                            sig._app = app
                            sig._window_title = title
                            log.info(
                                "Vision (OCR+AX): %.1fs, %d chars, %s",
                                ocr_elapsed, len(sig.text),
                                image_path.split("/")[-1],
                            )
                        else:
                            log.error(
                                "OCR returned empty for %s — deja-ocr "
                                "binary may be missing or the image is "
                                "unreadable. No fallback.",
                                image_path,
                            )
                            sig.text = "(OCR failed — no text extracted)"

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
