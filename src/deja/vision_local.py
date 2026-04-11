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


_PROMPT_TEMPLATE = (
    "This is {user_name}'s Mac. {app_context}"
    "\n\nRead this screenshot carefully. What app is {first_name} using? "
    "Read any visible messages, emails, or conversations — quote the "
    "actual text you can see. Who is talking to whom, and about what?"
)

_PROMPT_FALLBACK = (
    "Read this screenshot carefully. What apps are open? "
    "What names appear? What is the user doing? "
    "Quote any visible text."
)

# Cache the entity context so we don't re-read the wiki every 6 seconds
_entity_context_cache: str | None = None
_entity_context_ts: float = 0

# Lazy-loaded model state — survives for the process lifetime.
_model = None
_processor = None
_load_attempted = False


def _build_entity_context() -> str:
    """Compact list of known projects from the wiki index, cached 5min."""
    global _entity_context_cache, _entity_context_ts

    if _entity_context_cache and (time.time() - _entity_context_ts) < 300:
        return _entity_context_cache

    try:
        from deja.config import WIKI_DIR
        index_path = WIKI_DIR / "index.md"
        if not index_path.exists():
            return ""

        projects: list[str] = []
        section: str | None = None

        for line in index_path.read_text().splitlines():
            if line.startswith("## People"):
                section = "people"
            elif line.startswith("## Projects"):
                section = "projects"
            elif line.startswith("## "):
                section = None
            elif line.startswith("- [[") and section == "projects":
                slug = line.split("[[")[1].split("]]")[0]
                name = slug.replace("-", " ").title()
                if len(projects) < 20:
                    projects.append(name)

        parts = ["He uses Superhuman for email, Slack and WhatsApp for messaging."]
        if projects:
            parts.append(f"He works on projects including {', '.join(projects[:8])}.")

        _entity_context_cache = " ".join(parts)
        _entity_context_ts = time.time()
        return _entity_context_cache

    except Exception:
        return ""


def _build_prompt(voice_context: str = "") -> str:
    """Compose the vision prompt with optional identity grounding and voice context."""
    base = _PROMPT_FALLBACK
    try:
        from deja.identity import load_user
        user = load_user()
        if not user.is_generic:
            app_context = _build_entity_context()
            base = _PROMPT_TEMPLATE.format(
                user_name=user.name,
                first_name=user.first_name,
                app_context=app_context,
            )
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


def describe_screen_local(image_path: str, voice_context: str = "") -> str | None:
    """Describe a screenshot using on-device FastVLM via mlx-vlm.

    Args:
        image_path: Path to a PNG/JPEG screenshot file.
        voice_context: Optional recent voice dictation. If provided, the
            model treats it as the user's own commentary on the screen
            and grounds the description in their stated intent.

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

    prompt_text = _build_prompt(voice_context=voice_context)

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
