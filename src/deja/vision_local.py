"""On-device screenshot description via bundled llama.cpp + Qwen2.5-VL 3B.

Runs Qwen2.5-VL 3B locally through ``llama-mtmd-cli`` — no data leaves
the Mac. The model is downloaded from HuggingFace on first launch
(~2.5GB total) and cached under ``~/.deja/models/``.

This replaces the Gemini-based ``describe_screen()`` for the screenshot
observation pipeline. The text description (not the image) flows to the
integration cycle as before.

Why bundled binaries instead of mlx-vlm: see ``docs/llama-cpp-bundling.md``.
The public API of this module is intentionally unchanged from the
mlx-vlm version so callers in ``agent/observation_cycle.py`` don't
need to be touched.
"""

from __future__ import annotations

import logging
import subprocess
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
    """True iff the bundled llama-mtmd-cli binary is present."""
    return local_models.llama_binary("llama-mtmd-cli") is not None


def is_model_downloaded() -> bool:
    """True iff both the vision model and its mmproj are cached locally."""
    return local_models.is_vision_downloaded()


def get_download_status() -> dict:
    """Return the unified download/load status from local_models."""
    return local_models.get_status()


def download_model() -> bool:
    """Download all on-device models (vision + text).

    Despite the name, this kicks off the unified download driver in
    ``local_models``. Kept for backwards compatibility with the
    setup-panel API which used to call only the vision module.
    """
    return local_models.download_all()


def describe_screen_local(image_path: str, voice_context: str = "") -> str | None:
    """Describe a screenshot using on-device Qwen2.5-VL via llama-mtmd-cli.

    Args:
        image_path: Path to a PNG/JPEG screenshot file.
        voice_context: Optional recent voice dictation. If provided, the
            model treats it as the user's own commentary on the screen
            and grounds the description in their stated intent.

    Returns:
        Text description of what's on screen, or None if local vision
        is unavailable, the model isn't downloaded, or inference fails.
    """
    binary = local_models.llama_binary("llama-mtmd-cli")
    if binary is None:
        log.debug("llama-mtmd-cli not bundled — local vision unavailable")
        return None

    if not local_models.is_vision_downloaded():
        log.debug("Vision GGUF not downloaded yet")
        return None

    image = Path(image_path)
    if not image.exists():
        log.warning("Screenshot file missing: %s", image_path)
        return None

    prompt = _build_prompt(voice_context=voice_context)
    request_id = None
    try:
        from deja.telemetry import new_request_id
        request_id = new_request_id()
    except Exception:
        pass

    cmd = [
        str(binary),
        "-m", str(local_models.VISION_MODEL_PATH),
        "--mmproj", str(local_models.VISION_MMPROJ_PATH),
        "--image", str(image),
        "-p", prompt,
        "--n-predict", "300",
        "--temp", "0.1",
        "-ngl", "999",
        "--no-display-prompt",
    ]

    t0 = time.time()
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        log.warning("llama-mtmd-cli timed out on %s", image.name)
        _track_inference(False, time.time() - t0, 0, request_id, error="timeout")
        return None
    except Exception:
        log.exception("llama-mtmd-cli failed to launch")
        return None

    elapsed = time.time() - t0

    if result.returncode != 0:
        stderr_tail = (result.stderr or "")[-500:]
        log.warning(
            "llama-mtmd-cli exited %d in %.1fs: %s",
            result.returncode,
            elapsed,
            stderr_tail,
        )
        _track_inference(
            False, elapsed, len(result.stdout or ""), request_id,
            error=f"exit_{result.returncode}",
        )
        return None

    text = (result.stdout or "").strip()
    if not text:
        log.warning("llama-mtmd-cli returned empty output for %s", image.name)
        _track_inference(False, elapsed, 0, request_id, error="empty")
        return None

    log.info(
        "Vision (Qwen2.5-VL): %.1fs, %d chars, %s",
        elapsed,
        len(text),
        image.name,
    )
    _track_inference(True, elapsed, len(text), request_id)
    return text


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
