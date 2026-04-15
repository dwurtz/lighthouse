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


# Two grounding discoveries from the 2026-04-09 eval (50% must-hit,
# zero hallucinations) and the 2026-04-11 regression:
#
# 1. FastVLM 0.5B LIKES a compact, natural-language project hint
#    ("He works on projects including Deja, Blade and Rose, ...").
#    It uses the names as a disambiguation cue when content on screen
#    is ambiguous — that's what got must-hit from ~30% (no context) up
#    to 50% on fixtures.
#
# 2. FastVLM 0.5B HATES a raw index.md slug dump. Given 50 lines of
#    `- [[ship-new-blade-rose-theme]] — ...`, the tiny model loses
#    track of the image and regurgitates slugs into its description.
#    That's how "Autonomous Carpooling Platform for 2014 Utah Royals
#    FC-AZ Pre-ECNL at 5901-e-Valley-vista property management" ended
#    up in the output for a plain Messages window on 2026-04-11.
#
# So the grounding format matters as much as the content: prose list
# of 8 project names = helpful hint, raw 50-line slug catalog =
# catastrophic distraction. See commit 44e2675 for the regression and
# bfd756f for the original working prompt.
_MAX_PROJECTS_IN_PROMPT = 8


def _format_project_hint() -> str:
    """Return a one-sentence project hint like
    ``"He works on projects including Deja, Blade and Rose, ..."``.

    Walks ``~/Deja/projects/`` directly and picks the top
    ``_MAX_PROJECTS_IN_PROMPT`` projects by mtime. Not index.md: the
    index became a flat recency list in commit e2630bf so there's no
    longer a "## Projects" section to parse. The projects directory
    is still structured and gives us exactly what we want. Returns an
    empty string if the dir isn't available — we never want this to
    block the vision call.
    """
    try:
        from deja.config import WIKI_DIR

        projects_dir = WIKI_DIR / "projects"
        if not projects_dir.is_dir():
            return ""

        entries = sorted(
            projects_dir.glob("*.md"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:_MAX_PROJECTS_IN_PROMPT]
        if not entries:
            return ""

        names = [p.stem.replace("-", " ").title() for p in entries]
        # Don't assume which apps the user uses — let the vision model
        # identify apps from pixels + AX context. Only list their
        # active projects.
        return f"Their active projects include: {', '.join(names)}."
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# 3-pass multi-prompt strategy
#
# Each screenshot gets 3 sequential FastVLM calls, each with a different
# prompt that forces the model to attend to a different layer of the
# image. The union of all 3 descriptions reaches 95.5% must-hit on the
# 15-fixture eval suite — up from 36% single-pass — because each lens
# catches details the others miss:
#
#   Pass 0 (OCR):      raw text extraction — names, tickers, URLs, timestamps
#   Pass 1 (people):   who is on screen, grounded by wiki people list
#   Pass 2 (activity): what David is doing — app, task, project context
#
# Total time: ~5s for all 3 passes at 1200px on Apple Silicon.
# See the 2026-04-12 eval session for the full A/B history.
# ---------------------------------------------------------------------------

_VISION_RESIZE_WIDTH = 1200

# ---------------------------------------------------------------------------
# macOS Vision OCR — extract raw text from screenshots
#
# Apple's VNRecognizeTextRequest is fast (~1.5s compiled), accurate,
# runs on-device, and requires no TCC permissions (it operates on image
# files, not cameras). The text it extracts is prepended to each
# FastVLM pass so the model doesn't need to OCR from pixels — it just
# describes the layout and context around text that's already been
# perfectly transcribed.
#
# The `deja-ocr` binary is compiled from menubar/Sources/Tools/deja-ocr.swift
# and bundled inside Deja.app/Contents/MacOS/. When running from the
# dev tree, falls back to /tmp/deja-ocr (compiled by `make test-swift`).
# ---------------------------------------------------------------------------

_OCR_TIMEOUT_S = 10


def _find_ocr_binary() -> str | None:
    """Locate the deja-ocr binary — bundled app or dev fallback."""
    import shutil

    # Bundled inside the app
    candidates = [
        Path(__file__).resolve().parents[3]
        / "Contents"
        / "MacOS"
        / "deja-ocr",  # when running from bundled python-env
    ]
    # Also check common bundle paths
    for app_dir in [
        Path("/Applications/Deja.app/Contents/MacOS/deja-ocr"),
        Path.home() / "Library/Developer/Xcode/DerivedData" / "Deja-temp/Build/Products/Release/deja-ocr",
    ]:
        candidates.append(app_dir)
    # Dev fallback
    candidates.append(Path("/tmp/deja-ocr"))

    for p in candidates:
        if p.exists() and p.is_file():
            return str(p)

    # Last resort: check PATH
    found = shutil.which("deja-ocr")
    return found


def _focused_region_from_sidecar(image_path: str) -> tuple[float, float, float, float] | None:
    """Look for the screen_<N>_ax.json sidecar matching this screenshot
    and return its focused_frame_norm as (x, y, w, h) with top-left
    origin. Returns None when no sidecar exists, no focused frame is
    recorded, or the values look implausible (e.g. covers the whole
    display, in which case cropping is a no-op anyway).
    """
    import json
    import os
    import re

    m = re.match(r"screen_(\d+)\.png$", os.path.basename(image_path))
    if not m:
        return None
    sidecar = os.path.join(os.path.dirname(image_path), f"screen_{m.group(1)}_ax.json")
    if not os.path.exists(sidecar):
        return None
    try:
        with open(sidecar) as f:
            data = json.load(f)
    except Exception:
        return None
    frame = data.get("focused_frame_norm")
    if not isinstance(frame, dict):
        return None
    try:
        x, y, w, h = float(frame["x"]), float(frame["y"]), float(frame["w"]), float(frame["h"])
    except (KeyError, TypeError, ValueError):
        return None
    # Skip cropping when the focused window already covers ~the whole
    # display; the crop adds no value and the region clamps in deja-ocr
    # would just no-op.
    if w * h > 0.95:
        return None
    if w <= 0 or h <= 0:
        return None
    return (x, y, w, h)


def ocr_screen(image_path: str) -> str:
    """Extract text from a screenshot using macOS Vision OCR.

    When a screen_<N>_ax.json sidecar with focused_frame_norm exists
    next to the screenshot, OCR is restricted to the focused window's
    bounds. Eliminates sidebar / menu-bar / dock noise that produces
    phantom entities downstream.

    Returns the recognized text as a single string (one line per
    recognized text block). Returns an empty string on any failure —
    never blocks the vision pipeline.
    """
    import subprocess

    binary = _find_ocr_binary()
    if not binary:
        log.debug("deja-ocr binary not found — skipping OCR")
        return ""

    cmd = [binary, image_path]
    region = _focused_region_from_sidecar(image_path)
    if region is not None:
        x, y, w, h = region
        cmd += ["--region", f"{x:.4f}", f"{y:.4f}", f"{w:.4f}", f"{h:.4f}"]

    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_OCR_TIMEOUT_S,
        )
        if r.returncode != 0:
            log.debug("deja-ocr failed (rc=%d): %s", r.returncode, r.stderr[:200])
            return ""
        return (r.stdout or "").strip()
    except Exception:
        log.debug("deja-ocr subprocess failed", exc_info=True)
        return ""

# People hint — cached for 5 min like project hint
_people_hint_cache: str | None = None
_people_hint_ts: float = 0
_MAX_PEOPLE_IN_PROMPT = 15


def _format_people_hint() -> str:
    """Return a compact people list from the wiki's people/ directory."""
    global _people_hint_cache, _people_hint_ts

    if _people_hint_cache is not None and (time.time() - _people_hint_ts) < 300:
        return _people_hint_cache

    try:
        from deja.config import WIKI_DIR

        people_dir = WIKI_DIR / "people"
        if not people_dir.is_dir():
            return ""

        entries = sorted(
            people_dir.glob("*.md"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:_MAX_PEOPLE_IN_PROMPT]
        if not entries:
            return ""

        names = [p.stem.replace("-", " ").title() for p in entries]
        _people_hint_cache = "Key people: " + ", ".join(names) + "."
        _people_hint_ts = time.time()
        return _people_hint_cache
    except Exception:
        return ""


def _build_preamble(ax_context: dict | None = None) -> str:
    """Shared preamble for all 3 passes: identity + projects + AX app."""
    ctx = ax_context or {}
    app = ctx.get("app", "")
    title = ctx.get("window_title", "")

    if app and title:
        app_part = f" {app} open, showing \"{title}\""
    elif app:
        app_part = f" {app} open"
    else:
        app_part = " an app open"

    try:
        from deja.identity import load_user

        user = load_user()
        if not user.is_generic:
            return (
                f"{user.name} has{app_part} on his Mac."
                f" {_format_project_hint()}"
            )
    except Exception:
        pass
    return f"The user has{app_part}."


def _build_pass_prompts(
    voice_context: str = "",
    ax_context: dict | None = None,
    ocr_text: str = "",
) -> list[str]:
    """Return the 3 pass prompts (OCR, people, activity).

    When ``ocr_text`` is provided (from macOS Vision OCR), it's
    included in every pass so FastVLM can reference the already-
    extracted text instead of trying to read pixels. This is the
    key quality lever — Apple OCR reads text perfectly, FastVLM
    describes the layout and context.

    Voice context, when present, is prepended to every pass so all
    three lenses benefit from the user's stated intent.
    """
    pre = _build_preamble(ax_context)

    if ocr_text:
        # Truncate to avoid overwhelming FastVLM's context window.
        # 3000 chars ≈ 750 tokens — leaves plenty of budget for the
        # image and the task instruction.
        ocr_block = (
            f"\n\n# Text extracted from this screen (via OCR)\n\n"
            f"{ocr_text[:3000]}\n\n"
            f"# Your task\n\n"
        )
    else:
        ocr_block = "\n\n"

    passes = [
        # Pass 0: Content — what's on screen (OCR provides the text,
        # model describes the structure)
        f"{pre}{ocr_block}Using the extracted text above and the image, "
        f"describe what is on screen. Identify which app is shown, "
        f"what the main content is, and quote the key details.",
        # Pass 1: People — who is visible, grounded by wiki people list
        f"{pre} {_format_people_hint()}{ocr_block}Who is on this "
        f"screen? For each person visible, quote what they said or "
        f"what content is associated with them. Match names to the "
        f"key people list when possible.",
        # Pass 2: Activity — what David is doing
        f"{pre}{ocr_block}What is David doing right now? Describe "
        f"the activity: what app, what task, what content is he "
        f"looking at or working on? Name specific projects, files, "
        f"websites, or tools visible.",
    ]

    if voice_context:
        voice_block = (
            f"# IMPORTANT: The user just said this while looking at the screen\n\n"
            f'"{voice_context}"\n\n'
            f"Use their words as the primary lens. What they said reveals "
            f"their intent. Ground your description in their commentary.\n\n"
            f"---\n\n"
        )
        passes = [voice_block + p for p in passes]

    return passes


# Legacy single-prompt templates kept for describe_screen_local() fallback
_PROMPT_GROUNDED = (
    "This is {user_name}'s Mac. {project_hint}"
    "{app_sentence}"
    "\n\nRead the content on screen carefully. Quote any visible "
    "text verbatim — names, subject lines, messages, numbers, "
    "URLs, filenames, timestamps. What is on screen?"
)

_PROMPT_FALLBACK = (
    "Read this screenshot carefully. What apps are open? "
    "What names appear? What is the user doing? "
    "Quote any visible text."
)

# Lazy-loaded model state — survives for the process lifetime.
_model = None
_processor = None
_load_attempted = False


def _format_app_sentence(ax_context: dict | None) -> str:
    """Turn AX context into a natural-language sentence like
    ``" He has Messages open, showing 'Molly/Ruby Carpool'."``.

    If AX context is empty (no accessibility access, or no frontmost
    app detected), returns an empty string — never a dangling partial
    sentence. The sentence starts with a space so it can be appended
    directly after the project hint without awkward whitespace.
    """
    ctx = ax_context or {}
    app = ctx.get("app", "")
    title = ctx.get("window_title", "")
    if not app:
        return ""
    if title:
        return f" He has {app} open, showing \"{title}\"."
    return f" He has {app} open."


def _build_prompt(
    voice_context: str = "",
    ax_context: dict | None = None,
) -> str:
    """Compose the vision prompt with optional grounding blocks.

    Layers (all skip-if-empty — missing data produces no prompt bytes,
    not empty variable slots):

      - Voice context — what the user just dictated. Strongest single
        grounding signal we have; gets "the user just said this"
        framing to prime FastVLM to read the screen through that lens.
      - App sentence — frontmost app + window title from the macOS
        Accessibility API, woven into the prompt as natural language
        so FastVLM doesn't waste attention identifying the app from
        pixels. E.g. "He has Messages open, showing 'Molly/Ruby
        Carpool'."
      - Project hint — one-sentence list of the top 8 projects from
        the wiki index. Disambiguation cue, not a catalog dump.
    """
    base = _PROMPT_FALLBACK
    try:
        from deja.identity import load_user

        user = load_user()
        if not user.is_generic:
            base = _PROMPT_GROUNDED.format(
                user_name=user.name,
                project_hint=_format_project_hint(),
                app_sentence=_format_app_sentence(ax_context),
            )
        elif ax_context and ax_context.get("app"):
            base = _format_app_sentence(ax_context) + "\n\n" + _PROMPT_FALLBACK
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


def _resize_for_vision(image_path: str) -> str:
    """Resize a screenshot to ``_VISION_RESIZE_WIDTH`` px wide.

    Returns the path to a resized temp file (JPEG, q85). The caller
    is responsible for cleaning it up. If the image is already at or
    below the target width, returns the original path unchanged.
    """
    try:
        from PIL import Image as PILImage

        img = PILImage.open(image_path)
        if img.width <= _VISION_RESIZE_WIDTH:
            return image_path
        ratio = _VISION_RESIZE_WIDTH / img.width
        img = img.convert("RGB").resize(
            (_VISION_RESIZE_WIDTH, int(img.height * ratio)),
            PILImage.LANCZOS,
        )
        import tempfile

        tmp = tempfile.mktemp(suffix=".jpg")
        img.save(tmp, format="JPEG", quality=85)
        return tmp
    except Exception:
        log.debug("resize failed, using original", exc_info=True)
        return image_path


def describe_screen_multipass(
    image_path: str,
    voice_context: str = "",
    ax_context: dict | None = None,
) -> str | None:
    """Run 3 sequential FastVLM passes on one screenshot and return
    the combined description.

    Each pass uses a different prompt lens (OCR, people, activity) so
    the model attends to different layers of the image. The union of
    all 3 reaches ~95% must-hit on the 15-fixture eval suite vs ~36%
    for a single pass.

    The image is resized to ``_VISION_RESIZE_WIDTH`` (1200px) before
    inference — FastVLM's vision encoder handles this well (~1.7s per
    pass) and the extra pixels let it read text it can't resolve at
    800px.

    Returns the combined text from all successful passes joined by
    double newlines, or None if the model isn't available.
    """
    image = Path(image_path)
    if not image.exists():
        log.warning("Screenshot file missing: %s", image_path)
        return None

    if not _ensure_model():
        return None

    resized = _resize_for_vision(image_path)
    cleanup_resized = resized != image_path

    try:
        from mlx_vlm import generate
        from mlx_vlm.prompt_utils import apply_chat_template

        # Run macOS Vision OCR first — 1.5s for perfect text extraction.
        # The OCR text is fed into every FastVLM pass so the model
        # doesn't need to read pixels, just describe layout + context.
        t_ocr = time.time()
        ocr_text = ocr_screen(image_path)
        if ocr_text:
            log.info("Vision OCR: %.1fs, %d chars", time.time() - t_ocr, len(ocr_text))

        pass_prompts = _build_pass_prompts(
            voice_context=voice_context,
            ax_context=ax_context,
            ocr_text=ocr_text,
        )

        descriptions: list[str] = []
        total_elapsed = 0.0
        pass_labels = ["OCR", "people", "activity"]

        for i, prompt_text in enumerate(pass_prompts):
            try:
                prompt = apply_chat_template(
                    _processor,
                    config=_model.config,
                    prompt=f"<image>\n{prompt_text}",
                    images=[resized],
                )

                t0 = time.time()
                result = generate(
                    _model, _processor, prompt, [resized],
                    max_tokens=300, temperature=0.3,
                )
                elapsed = time.time() - t0
                total_elapsed += elapsed

                text = (result.text or "").strip()
                if text:
                    descriptions.append(text)
                    log.debug(
                        "Vision pass %s: %.1fs, %d chars",
                        pass_labels[i], elapsed, len(text),
                    )
                else:
                    log.debug("Vision pass %s: empty output", pass_labels[i])
            except Exception:
                log.debug(
                    "Vision pass %s failed", pass_labels[i], exc_info=True
                )

        if not descriptions:
            log.warning("All 3 vision passes returned empty for %s", image.name)
            return None

        combined = "\n\n".join(descriptions)
        log.info(
            "Vision (FastVLM 3-pass): %.1fs total, %d chars, %s",
            total_elapsed, len(combined), image.name,
        )

        try:
            from deja.telemetry import new_request_id, track

            track("local_inference_vision", {
                "ok": True,
                "duration_ms": int(total_elapsed * 1000),
                "output_chars": len(combined),
                "passes": len(descriptions),
                "request_id": new_request_id(),
            })
        except Exception:
            pass

        return combined

    except Exception:
        log.exception("FastVLM multipass failed for %s", image.name)
        return None
    finally:
        if cleanup_resized:
            try:
                import os
                os.remove(resized)
            except OSError:
                pass


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
