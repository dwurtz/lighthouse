"""On-device integration cycle using Qwen3 8B via mlx-lm.

Runs the integration analysis locally on Apple Silicon — no data leaves
the Mac. The model is downloaded from HuggingFace on first use (~5GB)
and cached for subsequent runs.

This replaces the Gemini-based integrate_observations() call for the
analysis cycle. Uses the prompt at ~/Deja/prompts/integrate_local.md
(tuned to match Gemini's update volume on Qwen3 8B).
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Qwen3 8B instruct, 4-bit quantized — ~5GB on disk, ~6GB in RAM
_MODEL_ID = "mlx-community/Qwen3-8B-4bit"

# Lazy-loaded model and tokenizer
_model = None
_tokenizer = None
_load_attempted = False


def _ensure_model() -> bool:
    """Load Qwen3 model on first call. Cached for the process lifetime."""
    global _model, _tokenizer, _load_attempted

    if _model is not None:
        return True
    if _load_attempted:
        return False

    _load_attempted = True
    try:
        from mlx_lm import load
        log.info("Loading Qwen3 8B (first use may download ~5GB)...")
        t0 = time.time()
        _model, _tokenizer = load(_MODEL_ID)
        log.info("Qwen3 loaded in %.1fs", time.time() - t0)
        return True
    except ImportError:
        log.warning("mlx-lm not installed — local integration disabled")
        return False
    except Exception:
        log.exception("Failed to load Qwen3 model")
        return False


def is_available() -> bool:
    """Check if local integration is available (mlx-lm installed)."""
    try:
        import mlx_lm  # noqa: F401
        return True
    except ImportError:
        return False


def is_model_downloaded() -> bool:
    """Check if the Qwen3 model weights are already cached locally."""
    try:
        from huggingface_hub import try_to_load_from_cache
        result = try_to_load_from_cache(_MODEL_ID, "config.json")
        return result is not None
    except Exception:
        return False


_download_progress: dict = {
    "status": "idle",
    "progress": 0.0,
    "message": "",
    "model_id": _MODEL_ID,
    "model_size_mb": 5000,
}


def get_download_status() -> dict:
    """Return the current model download/load status."""
    return dict(_download_progress)


def download_model() -> bool:
    """Download and pre-load the Qwen3 model.

    Called during setup to ensure the model is ready before the app
    needs to run an integration cycle.
    """
    global _model, _tokenizer, _load_attempted

    if _model is not None:
        _download_progress["status"] = "ready"
        _download_progress["progress"] = 1.0
        return True

    _download_progress["status"] = "downloading"
    _download_progress["progress"] = 0.1
    _download_progress["message"] = "Downloading Qwen3 8B (~5 GB)..."

    try:
        from mlx_lm import load
        _download_progress["progress"] = 0.3
        _model, _tokenizer = load(_MODEL_ID)
        _load_attempted = True

        _download_progress["status"] = "ready"
        _download_progress["progress"] = 1.0
        _download_progress["message"] = "Model ready"
        log.info("Qwen3 downloaded and loaded successfully")
        return True
    except ImportError:
        _download_progress["status"] = "error"
        _download_progress["message"] = "mlx-lm not installed"
        return False
    except Exception as e:
        _download_progress["status"] = "error"
        _download_progress["message"] = str(e)[:200]
        log.exception("Qwen3 download failed")
        return False


def _parse_json(raw: str) -> dict | None:
    """Best-effort JSON extraction from model output."""
    text = raw.strip()
    # Strip markdown fences
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    # Strip Qwen thinking tags
    if "<think>" in text and "</think>" in text:
        text = text.split("</think>", 1)[1].strip()
    # Find outermost JSON object
    if "{" in text and "}" in text:
        start = text.index("{")
        end = text.rindex("}") + 1
        try:
            return json.loads(text[start:end])
        except json.JSONDecodeError:
            pass
    return None


def integrate_observations_local(
    signals_text: str,
    wiki_text: str,
) -> dict | None:
    """Run one integration cycle using Qwen3 8B locally.

    Returns a dict with {reasoning, wiki_updates} matching the format
    of gemini.integrate_observations(). Returns None if local
    inference is unavailable or fails.

    Args:
        signals_text: Formatted observation batch from the collector.
        wiki_text: Relevant wiki context for grounding.
    """
    if not _ensure_model():
        return None

    try:
        from mlx_lm import generate
        from datetime import datetime
        from deja.prompts import load as load_prompt
        from deja.identity import load_user
        from deja.wiki_schema import load_schema

        # Load the local-tuned prompt template (with few-shot example)
        try:
            template = load_prompt("integrate_local")
        except Exception:
            template = load_prompt("integrate")

        now = datetime.now()
        hour = now.hour
        if hour < 12:
            tod = "morning"
        elif hour < 17:
            tod = "afternoon"
        else:
            tod = "evening"

        user_fields = load_user().as_prompt_fields()
        schema = load_schema()

        from deja.config import WIKI_DIR
        goals_path = WIKI_DIR / "goals.md"
        goals_text = goals_path.read_text() if goals_path.exists() else "(no goals.md)"

        try:
            from deja.observations.contacts import get_contacts_summary
            contacts_text = get_contacts_summary()
        except Exception:
            contacts_text = "(contacts unavailable)"

        prompt = template.format(
            current_time=now.strftime("%Y-%m-%d %H:%M"),
            day_of_week=now.strftime("%A"),
            time_of_day=tod,
            contacts_text=contacts_text,
            schema=schema,
            goals=goals_text,
            wiki_text=wiki_text or "(empty)",
            signals_text=signals_text or "(no new signals)",
            **user_fields,
        )

        # Format as chat message for Qwen with thinking DISABLED
        # Qwen3 is a thinking model — without this, it wastes tokens on
        # internal reasoning and produces empty/truncated output.
        messages = [{"role": "user", "content": prompt}]
        try:
            chat_prompt = _tokenizer.apply_chat_template(
                messages, add_generation_prompt=True, tokenize=False,
                enable_thinking=False,
            )
        except TypeError:
            # Older tokenizers don't support enable_thinking
            chat_prompt = _tokenizer.apply_chat_template(
                messages, add_generation_prompt=True, tokenize=False,
            )

        t0 = time.time()
        response_text = generate(
            _model,
            _tokenizer,
            prompt=chat_prompt,
            max_tokens=4096,
            verbose=False,
        )
        elapsed = time.time() - t0

        # Parse JSON from response
        result = _parse_json(response_text)
        if not result:
            log.warning("Qwen3 integration returned unparseable output")
            return None

        result.setdefault("reasoning", "")
        result.setdefault("wiki_updates", [])

        log.info(
            "Qwen3 integration: %.1fs, %d updates, reasoning=%s",
            elapsed,
            len(result.get("wiki_updates", [])),
            result.get("reasoning", "")[:100],
        )

        return result

    except Exception:
        log.exception("Qwen3 integration failed")
        return None
