"""On-device screenshot description using Apple FastVLM via mlx-vlm.

Runs FastVLM 0.5B locally on Apple Silicon — no data leaves the Mac.
The model is downloaded from HuggingFace on first use (~500MB) and
cached for subsequent runs.

This replaces the Gemini-based describe_screen() for the screenshot
observation pipeline. The text description (not the image) flows
to the integration cycle as before.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

log = logging.getLogger(__name__)

_PROMPT_TEMPLATE = (
    "This is {user_name}'s Mac. {app_context}"
    "\n\nRead this screenshot carefully. What app is {first_name} using? "
    "Read any visible messages, emails, or conversations — quote the "
    "actual text you can see. Who is talking to whom, and about what?"
)

# Fallback if identity isn't available
_PROMPT_FALLBACK = (
    "Read this screenshot carefully. What apps are open? "
    "What names appear? What is the user doing? "
    "Quote any visible text."
)

# Cache the entity context so we don't re-read the wiki every 6 seconds
_entity_context_cache: str | None = None
_entity_context_ts: float = 0


def _build_entity_context() -> str:
    """Build a compact list of known people and projects from the wiki index.

    Cached for 5 minutes to avoid re-reading on every screenshot.
    Returns a string like: "Known people: Ami Vora, Amanda Peffer, ...
    Known projects: Deja, Blade and Rose, ..."
    """
    global _entity_context_cache, _entity_context_ts

    if _entity_context_cache and (time.time() - _entity_context_ts) < 300:
        return _entity_context_cache

    try:
        from deja.config import WIKI_DIR
        index_path = WIKI_DIR / "index.md"
        if not index_path.exists():
            return ""

        people = []
        projects = []
        section = None

        for line in index_path.read_text().splitlines():
            if line.startswith("## People"):
                section = "people"
            elif line.startswith("## Projects"):
                section = "projects"
            elif line.startswith("## "):
                section = None
            elif line.startswith("- [[") and section:
                # Extract the display name from [[slug]] — Name
                slug = line.split("[[")[1].split("]]")[0]
                name = slug.replace("-", " ").title()
                if section == "people" and len(people) < 50:
                    people.append(name)
                elif section == "projects" and len(projects) < 20:
                    projects.append(name)

        parts = []
        parts.append("He uses Superhuman for email, Slack and WhatsApp for messaging.")
        if projects:
            parts.append(f"He works on projects including {', '.join(projects[:8])}.")

        _entity_context_cache = " ".join(parts)
        _entity_context_ts = time.time()
        return _entity_context_cache

    except Exception:
        return ""

_MODEL_ID = "apple/FastVLM-0.5B"

# Lazy-loaded model and processor
_model = None
_processor = None
_load_attempted = False


def _ensure_model():
    """Load FastVLM model on first call. Cached for the process lifetime."""
    global _model, _processor, _load_attempted

    if _model is not None:
        return True
    if _load_attempted:
        return False  # Already failed once, don't retry

    _load_attempted = True
    try:
        from mlx_vlm import load
        log.info("Loading FastVLM 0.5B (first use may download ~500MB)...")
        t0 = time.time()
        _model, _processor = load(_MODEL_ID)
        log.info("FastVLM loaded in %.1fs", time.time() - t0)
        return True
    except ImportError:
        log.warning("mlx-vlm not installed — local vision disabled. Install with: pip install mlx-vlm")
        return False
    except Exception:
        log.exception("Failed to load FastVLM model")
        return False


def describe_screen_local(image_path: str) -> str | None:
    """Describe a screenshot using on-device FastVLM.

    Args:
        image_path: Path to a PNG/JPEG screenshot file.

    Returns:
        Text description of what's on screen, or None if local
        vision is unavailable.
    """
    if not _ensure_model():
        return None

    try:
        from mlx_vlm import generate
        from mlx_vlm.prompt_utils import apply_chat_template

        # Build grounded prompt with user identity + wiki entities
        prompt_text = _PROMPT_FALLBACK
        try:
            from deja.identity import load_user
            user = load_user()
            if not user.is_generic:
                app_context = _build_entity_context()
                prompt_text = _PROMPT_TEMPLATE.format(
                    user_name=user.name,
                    first_name=user.first_name,
                    app_context=app_context,
                )
        except Exception:
            pass

        prompt = apply_chat_template(
            _processor,
            config=_model.config,
            prompt=f"<image>\n{prompt_text}",
            images=[image_path],
        )

        t0 = time.time()
        result = generate(
            _model, _processor, prompt, [image_path],
            max_tokens=300, temperature=0.1,
        )
        elapsed = time.time() - t0

        text = (result.text or "").strip()
        log.info(
            "FastVLM described screen in %.1fs (%d tokens, %.1f tok/s)",
            elapsed,
            result.generation_tokens,
            result.generation_tps,
        )
        return text if text else None

    except Exception:
        log.exception("FastVLM inference failed for %s", image_path)
        return None


def is_available() -> bool:
    """Check if local vision is available (mlx-vlm installed)."""
    try:
        import mlx_vlm  # noqa: F401
        return True
    except ImportError:
        return False


def is_model_downloaded() -> bool:
    """Check if the FastVLM model weights are already cached locally."""
    try:
        from huggingface_hub import try_to_load_from_cache
        # Check for the main model file
        result = try_to_load_from_cache(_MODEL_ID, "config.json")
        return result is not None and not isinstance(result, type(None))
    except Exception:
        return False


_download_progress: dict = {
    "status": "idle",  # idle, downloading, loading, ready, error
    "progress": 0.0,   # 0.0 to 1.0
    "message": "",
    "model_id": _MODEL_ID,
    "model_size_mb": 500,
}


def get_download_status() -> dict:
    """Return the current model download/load status."""
    return dict(_download_progress)


def download_model() -> bool:
    """Download and pre-load the FastVLM model.

    Called during setup to ensure the model is ready before the app
    starts capturing screenshots. Updates _download_progress for
    the UI to poll.
    """
    global _model, _processor, _load_attempted

    if _model is not None:
        _download_progress["status"] = "ready"
        _download_progress["progress"] = 1.0
        _download_progress["message"] = "Model ready"
        return True

    _download_progress["status"] = "downloading"
    _download_progress["progress"] = 0.1
    _download_progress["message"] = "Downloading FastVLM 0.5B (~500 MB)..."

    try:
        from mlx_vlm import load

        # The load() call handles download + cache + model init
        _download_progress["progress"] = 0.3
        _download_progress["message"] = "Downloading model weights..."

        _model, _processor = load(_MODEL_ID)
        _load_attempted = True

        _download_progress["status"] = "ready"
        _download_progress["progress"] = 1.0
        _download_progress["message"] = "Model ready"
        log.info("FastVLM model downloaded and loaded successfully")
        return True

    except ImportError:
        _download_progress["status"] = "error"
        _download_progress["message"] = "mlx-vlm not installed"
        log.warning("mlx-vlm not installed")
        return False
    except Exception as e:
        _download_progress["status"] = "error"
        _download_progress["message"] = str(e)[:200]
        log.exception("Model download failed")
        return False
