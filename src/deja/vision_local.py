"""On-device screenshot description via Apple FastVLM 0.5B + mlx-vlm.

Runs FastVLM 0.5B locally in-process using mlx-vlm — no data leaves
the Mac. Weights are downloaded from HuggingFace on first launch
(~1.4 GB) into ``~/Library/Application Support/com.deja.app/models/``.

This replaces the Gemini-based ``describe_screen()`` for the screenshot
observation pipeline. The text description (not the image) flows to the
integration cycle as before.

Performance target: ~3.5s per screenshot on Apple Silicon after the
model is warm. First call after backend start pays a one-time load
cost of ~8s.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from deja import local_models

log = logging.getLogger(__name__)


# How many head lines of index.md to inject into the vision prompt.
# index.md is recency-sorted and flat, so the first N lines are always
# the N most-recently-touched pages regardless of category. 50 is a
# sweet spot for FastVLM 0.5B: plenty of coverage of "what David is
# working on right now", while leaving attention budget for the image,
# the AX context block, and the task instructions. Triage and the
# integrate retrieval read the full index separately — they have
# larger context budgets (Flash-Lite 1M ctx) and benefit from seeing
# dormant entries that vision would just dilute on.
#
# Revisit this if FastVLM starts missing obvious entities in the tail
# (raise) or starts confusing hot entities with each other (lower).
_VISION_INDEX_HEAD_LINES = 50


# The grounded template uses ``{ax_block}`` as a placeholder for the
# Current UI context section. That block is either empty string (no AX
# data, no access, broken AX support) or a fully-formatted chunk
# ending in two newlines. Using a single interpolation slot means we
# never leave a dangling header when AX data is missing — the skip-if-
# empty discipline is enforced at the formatter, not here.
#
# The task instruction deliberately does NOT ask "what app is David
# using?" — the AX block already states the app name when available,
# and asking the model to re-derive it from pixels wastes attention
# that should go to content. The downstream integrate cycle needs
# specific facts (names, subject lines, numbers, quotes) to ground
# wiki updates, not a summary of app chrome.
_PROMPT_GROUNDED = (
    "This is {user_name}'s Mac.\n\n"
    "{ax_block}"
    "# People and projects {first_name} cares about\n\n"
    "{index_block}\n\n"
    "# Your task\n\n"
    "Describe what {first_name} is doing right now. Quote the specific "
    "details visible on screen — names, subject lines, message text, "
    "numbers, URLs, timestamps — so downstream reasoning has hard facts "
    "to work with, not a vague summary. Then explain why this matters: "
    "how does it connect to the people and projects above? What "
    "commitment, question, decision, or new information does this "
    "screenshot surface?"
)

_PROMPT_FALLBACK = (
    "Describe what the user is doing in this screenshot. Quote the "
    "specific details visible on screen — names, subject lines, "
    "message text, numbers, URLs, timestamps — so downstream reasoning "
    "has hard facts to work with. What commitment, question, decision, "
    "or new information does this screenshot surface?"
)

# Lazy-loaded model state — survives for the process lifetime.
_model = None
_processor = None
_load_attempted = False


def _build_prompt(
    voice_context: str = "",
    ax_context: dict | None = None,
) -> str:
    """Compose the vision prompt with optional grounding blocks.

    The prompt can include (in reverse order of priority):

      - Voice context — what the user just dictated, if within the
        lookback window. Forceful "the user just said this" framing.
      - Current UI context — frontmost app, focused window, focused
        widget, from the macOS Accessibility API. Skipped entirely
        (no header, no empty lines) when the AX dict is empty.
      - Wiki catalog — the full recency-sorted index.md so the model
        has names/descriptions for known people and projects.

    All three sections follow the same skip-if-empty discipline:
    missing data produces no prompt bytes, not empty variable slots.
    """
    from deja import ax_context as ax_mod

    ax_block = ax_mod.format_for_prompt(ax_context or {})

    base = _PROMPT_FALLBACK
    try:
        from deja.identity import load_user
        from deja.wiki_catalog import render_index_for_prompt

        user = load_user()
        if not user.is_generic:
            index_block = render_index_for_prompt(
                max_lines=_VISION_INDEX_HEAD_LINES,
                rebuild=False,
            ).strip() or "(no wiki entries yet)"
            base = _PROMPT_GROUNDED.format(
                user_name=user.name,
                first_name=user.first_name,
                ax_block=ax_block,
                index_block=index_block,
            )
        elif ax_block:
            # Generic identity path: no wiki grounding, but still
            # prepend the AX block if we have one — it's additive.
            base = ax_block + _PROMPT_FALLBACK
    except Exception:
        pass

    if voice_context:
        return (
            f"# IMPORTANT: The user just said this while looking at the screen\n\n"
            f'"{voice_context}"\n\n'
            f"Use their words as the primary lens. What they said reveals their intent. "
            f"Ground your description in their commentary.\n\n"
            f"---\n\n{base}"
        )
    return base


def is_available() -> bool:
    """True iff mlx_vlm can be imported."""
    try:
        import mlx_vlm  # noqa: F401
        return True
    except ImportError:
        return False


def is_model_downloaded() -> bool:
    """True iff FastVLM weights are cached locally."""
    return local_models.is_vision_downloaded()


def get_download_status() -> dict:
    """Return the unified download/load status from local_models."""
    return local_models.get_status()


def download_model() -> bool:
    """Download FastVLM 0.5B weights. Blocks until complete."""
    return local_models.download_all()


def _ensure_model() -> bool:
    """Load FastVLM into memory on first call. Cached for process lifetime."""
    global _model, _processor, _load_attempted

    if _model is not None:
        return True
    if _load_attempted:
        return False  # already failed once — don't thrash

    _load_attempted = True

    snapshot = local_models.fastvlm_path()
    if snapshot is None:
        log.debug("FastVLM weights not downloaded yet")
        return False

    try:
        from mlx_vlm import load
        log.info("Loading FastVLM 0.5B into memory...")
        t0 = time.time()
        _model, _processor = load(str(snapshot))
        log.info("FastVLM loaded in %.1fs", time.time() - t0)
        return True
    except ImportError:
        log.warning("mlx-vlm not installed — local vision disabled")
        return False
    except Exception:
        log.exception("Failed to load FastVLM")
        return False


def describe_screen_local(
    image_path: str,
    voice_context: str = "",
    ax_context: dict | None = None,
) -> str | None:
    """Describe a screenshot using on-device FastVLM via mlx-vlm.

    Args:
        image_path: Path to a PNG/JPEG screenshot file.
        voice_context: Optional recent voice dictation. If provided, the
            model treats it as the user's own commentary on the screen
            and grounds the description in their stated intent.
        ax_context: Optional macOS Accessibility context dict, typically
            from ``deja.ax_context.capture()``. Populated fields (app,
            window_title, focused_role, focused_label, focused_value)
            are rendered as a "Current UI context" block prepended to
            the task description. Empty or missing fields are silently
            skipped — no empty-slot injection.

    Returns:
        Text description, or None if local vision is unavailable,
        the model isn't downloaded, or inference fails.
    """
    image = Path(image_path)
    if not image.exists():
        log.warning("Screenshot file missing: %s", image_path)
        return None

    if not _ensure_model():
        return None

    prompt_text = _build_prompt(
        voice_context=voice_context,
        ax_context=ax_context,
    )

    request_id = None
    try:
        from deja.telemetry import new_request_id
        request_id = new_request_id()
    except Exception:
        pass

    try:
        from mlx_vlm import generate
        from mlx_vlm.prompt_utils import apply_chat_template

        prompt = apply_chat_template(
            _processor,
            config=_model.config,
            prompt=f"<image>\n{prompt_text}",
            images=[str(image)],
        )

        t0 = time.time()
        result = generate(
            _model, _processor, prompt, [str(image)],
            max_tokens=300, temperature=0.1,
        )
        elapsed = time.time() - t0

        text = (result.text or "").strip()
        if not text:
            log.warning("FastVLM returned empty output for %s", image.name)
            _track_inference(False, elapsed, 0, request_id, error="empty")
            return None

        log.info(
            "Vision (FastVLM): %.1fs, %d tokens, %.1f tok/s, %s",
            elapsed,
            getattr(result, "generation_tokens", 0),
            getattr(result, "generation_tps", 0.0) or 0.0,
            image.name,
        )
        _track_inference(True, elapsed, len(text), request_id)
        return text

    except Exception:
        log.exception("FastVLM inference failed for %s", image.name)
        _track_inference(False, 0.0, 0, request_id, error="exception")
        return None


def _track_inference(
    ok: bool,
    elapsed: float,
    output_chars: int,
    request_id: str | None,
    error: str | None = None,
) -> None:
    """Fire-and-forget telemetry for a single vision call."""
    try:
        from deja.telemetry import track
        props = {
            "ok": ok,
            "duration_ms": int(elapsed * 1000),
            "output_chars": output_chars,
        }
        if request_id:
            props["request_id"] = request_id
        if error:
            props["error"] = error
        track("local_inference_vision", props)
    except Exception:
        pass
