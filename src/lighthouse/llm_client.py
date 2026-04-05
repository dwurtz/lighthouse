"""Gemini LLM client for Lighthouse.

Uses the google-genai SDK for all LLM calls. All methods are async.
The API key is resolved via ``lighthouse.secrets.get_api_key()`` which
checks environment variables first (``GEMINI_API_KEY`` /
``GOOGLE_API_KEY``) and falls back to the macOS login keychain.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from google import genai
from google.genai import types

from lighthouse.config import INTEGRATE_MODEL, REFLECT_MODEL, VISION_MODEL

# Onboarding is a one-time, high-stakes run against potentially the
# user's entire digital history. Cost is amortized across the one run,
# so we use the strongest available model for maximum quality rather
# than the steady-state Flash-Lite used by the every-few-minutes cycle.
_ONBOARD_MODEL = REFLECT_MODEL
from lighthouse.prompts import load as load_prompt

log = logging.getLogger(__name__)


def _parse_json(raw: str) -> Any:
    """Best-effort JSON extraction from LLM output."""
    text = raw.strip()
    # Strip markdown fences
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    # Find outermost JSON structure
    for open_ch, close_ch in [("[", "]"), ("{", "}")]:
        if open_ch in text and close_ch in text:
            start = text.index(open_ch)
            end = text.rindex(close_ch) + 1
            try:
                return json.loads(text[start:end])
            except json.JSONDecodeError:
                continue
    return json.loads(text)


def _ensure_api_key_in_env() -> None:
    """Populate os.environ['GEMINI_API_KEY'] from the keychain if not set.

    The google-genai SDK reads the key from the environment at
    ``genai.Client()`` construction time. When Lighthouse is launched
    from the Swift menu-bar app (launchd-inherited environment), env
    vars are NOT populated from the user's shell profile — so we have
    to fetch the key from the keychain and set it explicitly before
    the SDK initializes its client.

    This runs ONCE at module import time and again at GeminiClient
    construction as a safety net. ``secrets.get_api_key()`` is cached
    so repeat calls don't spawn fresh ``security`` subprocesses.
    """
    import os
    if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
        return
    from lighthouse.secrets import get_api_key
    key = get_api_key()
    if key:
        os.environ["GEMINI_API_KEY"] = key


# Pre-populate at import time so any code that reads os.environ
# directly (the google-genai SDK, tests, subprocess.Popen spawns) sees
# the key before it's needed.
_ensure_api_key_in_env()


class GeminiClient:
    """Async wrapper around the google-genai SDK for all agent LLM operations."""

    def __init__(self) -> None:
        _ensure_api_key_in_env()
        self.client = genai.Client()

    # ------------------------------------------------------------------
    # Unified analyze + write (one LLM call per cycle)
    # ------------------------------------------------------------------

    async def integrate_observations(
        self,
        signals_text: str,
        wiki_text: str,
    ) -> dict:
        """Run one unified analysis cycle. Returns {reasoning, wiki_updates}."""
        now = datetime.now()
        current_time = now.strftime("%Y-%m-%d %H:%M")
        day_of_week = now.strftime("%A")
        hour = now.hour
        if hour < 12:
            time_of_day = "morning"
        elif hour < 17:
            time_of_day = "afternoon"
        else:
            time_of_day = "evening"

        try:
            from lighthouse.observations.contacts import get_contacts_summary
            contacts_text = get_contacts_summary()
        except Exception:
            contacts_text = "(contacts unavailable)"

        from lighthouse.wiki_schema import load_schema
        schema = load_schema()

        from lighthouse.identity import load_user
        user_fields = load_user().as_prompt_fields()

        prompt = load_prompt("integrate").format(
            current_time=current_time,
            day_of_week=day_of_week,
            time_of_day=time_of_day,
            contacts_text=contacts_text,
            schema=schema,
            wiki_text=wiki_text or "(empty)",
            signals_text=signals_text or "(no new signals)",
            **user_fields,
        )

        resp = await self.client.aio.models.generate_content(
            model=INTEGRATE_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                max_output_tokens=16384,
                temperature=0.2,
            ),
        )
        resp_text = resp.text

        try:
            result = json.loads(resp_text)
        except (json.JSONDecodeError, ValueError):
            result = _parse_json(resp_text)

        if not isinstance(result, dict):
            raise TypeError(f"integrate_observations expected dict, got {type(result).__name__}: {resp_text[:200]}")

        result.setdefault("reasoning", "")
        result.setdefault("wiki_updates", [])
        return result

    # ------------------------------------------------------------------
    # Onboarding — cold-start wiki generation from historical mail
    # ------------------------------------------------------------------

    async def onboard_from_observations(
        self,
        signals_text: str,
        wiki_text: str,
    ) -> dict:
        """One onboarding LLM call. Same I/O contract as ``integrate_observations``.

        Uses the dedicated ``onboard`` prompt which is tuned for bootstrapping
        a nearly-empty wiki from a large batch of historical threads and
        per-contact conversation digests — it errs toward creating pages
        rather than skipping, preserves existing content on updates, and
        treats group chats as context rather than auto-creating projects.
        Runs on the strongest available model (``REFLECT_MODEL`` / Pro)
        since onboarding is a one-time high-stakes call; the steady-state
        2-minute cycle still uses Flash-Lite via ``integrate_observations``.
        Output JSON shape matches integrate so ``wiki.apply_updates`` is
        reused unchanged.
        """
        now = datetime.now()
        current_time = now.strftime("%Y-%m-%d %H:%M")
        day_of_week = now.strftime("%A")
        hour = now.hour
        if hour < 12:
            time_of_day = "morning"
        elif hour < 17:
            time_of_day = "afternoon"
        else:
            time_of_day = "evening"

        try:
            from lighthouse.observations.contacts import get_contacts_summary
            contacts_text = get_contacts_summary()
        except Exception:
            contacts_text = "(contacts unavailable)"

        from lighthouse.wiki_schema import load_schema
        schema = load_schema()

        from lighthouse.identity import load_user
        user_fields = load_user().as_prompt_fields()

        prompt = load_prompt("onboard").format(
            current_time=current_time,
            day_of_week=day_of_week,
            time_of_day=time_of_day,
            contacts_text=contacts_text,
            schema=schema,
            wiki_text=wiki_text or "(empty — this is the first onboarding batch)",
            signals_text=signals_text or "(no threads in this batch)",
            **user_fields,
        )

        resp = await self.client.aio.models.generate_content(
            model=_ONBOARD_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                max_output_tokens=16384,
                temperature=0.2,
            ),
        )
        resp_text = resp.text

        try:
            result = json.loads(resp_text)
        except (json.JSONDecodeError, ValueError):
            result = _parse_json(resp_text)

        if not isinstance(result, dict):
            raise TypeError(
                f"onboard_from_observations expected dict, got "
                f"{type(result).__name__}: {resp_text[:200]}"
            )

        result.setdefault("reasoning", "")
        result.setdefault("wiki_updates", [])
        return result

    # ------------------------------------------------------------------
    # Screenshot analysis
    # ------------------------------------------------------------------

    async def describe_screen(
        self, image_path: str, goals_text: str = ""
    ) -> dict:
        """Describe a screenshot via Gemini Flash vision.

        The prompt is grounded in the current wiki `index.md` so the
        model can name specific entities when they appear on screen
        ("this Zillow listing relates to [[palo-alto-relocation]]")
        instead of producing generic descriptions.

        Returns {summary, app, key_details}.
        """
        # Resize to reduce payload (max 800px wide, JPEG q75)
        try:
            from PIL import Image
            import io

            img = Image.open(image_path).convert("RGB")
            if img.width > 800:
                ratio = 800 / img.width
                img = img.resize((800, int(img.height * ratio)), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=75)
            image_bytes = buf.getvalue()
            mime = "image/jpeg"
        except Exception:
            image_bytes = Path(image_path).read_bytes()
            mime = "image/png"

        # Load the vision prompt, injecting the current wiki index so the
        # model can ground its description in the user's actual entities,
        # plus the user's first name so it can address them correctly.
        from lighthouse.llm.prefilter import load_index_md
        from lighthouse.identity import load_user
        index_md = load_index_md().strip() or "(no wiki entries yet)"
        user_fields = load_user().as_prompt_fields()
        template = load_prompt("describe_screen")
        try:
            prompt = template.format(index_md=index_md, **user_fields)
        except KeyError:
            # Prompt doesn't use one of the expected placeholders — render
            # as-is rather than blowing up on a partial template.
            prompt = template

        # Vision uses its own model (VISION_MODEL) chosen by the
        # tools/vision_eval.py harness — Flash in the default config
        # because it produces ~4x more wiki-link grounding than Flash-Lite
        # on real fixtures and outperforms Pro on every aggregate metric
        # at 1/4 the cost. Max tokens bumped to 1024 because Flash is
        # more verbose than Flash-Lite and 800 was truncating some frames.
        resp = await self.client.aio.models.generate_content(
            model=VISION_MODEL,
            contents=[
                types.Part.from_bytes(data=image_bytes, mime_type=mime),
                prompt,
            ],
            config=types.GenerateContentConfig(
                max_output_tokens=1024,
                temperature=0.2,
            ),
        )

        if not resp.text:
            log.warning("Vision returned empty response (model=%s)", VISION_MODEL)
            return {"summary": "Screenshot analysis failed", "app": "", "key_details": ""}

        return {"summary": resp.text.strip()[:1000], "app": "", "key_details": ""}

    # ------------------------------------------------------------------
    # Audio transcription
    # ------------------------------------------------------------------

    async def transcribe_audio(self, wav_path: str) -> str:
        """Transcribe a WAV file via Gemini Flash.

        Uses ``gemini-2.5-flash`` in its normal multimodal mode — the
        ``*-native-audio-*`` variants are tied to the bidirectional Live
        API and can't be called via ``generateContent``. Regular Flash
        accepts ``audio/wav`` parts directly.

        Returns the transcript as a single string, or an empty string if
        the audio was silent / unintelligible / empty. Raw response is
        logged at INFO level for diagnostic purposes.
        """
        try:
            audio_bytes = Path(wav_path).read_bytes()
        except OSError as e:
            log.warning("transcribe_audio: could not read %s: %s", wav_path, e)
            return ""

        log.info("transcribe_audio: %s (%d bytes)", wav_path, len(audio_bytes))

        prompt = (
            "Transcribe the speech in this audio verbatim. Return ONLY the "
            "spoken words as plain text — no timestamps, no speaker labels, "
            "no commentary, no quotation marks, no preamble, no WebVTT. "
            "If there is no intelligible speech, return an empty string."
        )

        resp = await self.client.aio.models.generate_content(
            model="gemini-2.5-flash",
            contents=[
                types.Part.from_bytes(data=audio_bytes, mime_type="audio/wav"),
                prompt,
            ],
            config=types.GenerateContentConfig(
                max_output_tokens=2048,
                temperature=0.0,
            ),
        )
        raw = resp.text or ""
        log.info("transcribe_audio raw response: %r", raw[:500])

        transcript = raw.strip()
        # Strip any stray quotation marks or "no speech" filler from the
        # handful of cases where the model ignores the prompt.
        if transcript.lower() in ("empty", "(empty)", "no speech", "no speech detected", ""):
            return ""
        return transcript.strip('"').strip("'").strip()
