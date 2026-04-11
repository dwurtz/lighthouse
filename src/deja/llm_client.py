"""Gemini LLM client for Déjà.

Routes all LLM calls through the Deja API server proxy. When the
``GEMINI_API_KEY`` env var is set, falls back to direct google-genai
SDK calls (developer / CI mode).

The server proxy is at ``DEJA_API_URL`` (default ``https://api.trydeja.com``)
and accepts ``POST /v1/generate`` with ``Authorization: Bearer <token>``.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from google.genai import types  # re-exported for reflection.py and prefilter.py

from deja.auth import get_auth_token
from deja.config import INTEGRATE_MODEL, REFLECT_MODEL, VISION_MODEL

DEJA_API_URL = os.environ.get("DEJA_API_URL", "https://deja-api.onrender.com")
_USE_DIRECT = bool(os.environ.get("GEMINI_API_KEY"))

# Onboarding is a one-time, high-stakes run against potentially the
# user's entire digital history. Cost is amortized across the one run,
# so we use the strongest available model for maximum quality rather
# than the steady-state Flash-Lite used by the every-few-minutes cycle.
_ONBOARD_MODEL = REFLECT_MODEL
from deja.prompts import load as load_prompt

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


class GeminiClient:
    """Async wrapper for all agent LLM operations.

    In production, routes requests through the Deja API server proxy.
    When ``GEMINI_API_KEY`` is set, uses the google-genai SDK directly
    (developer / CI fallback).
    """

    def __init__(self) -> None:
        if _USE_DIRECT:
            from google import genai
            self._direct_client = genai.Client()
            self._http = None
        else:
            self._direct_client = None
            # 300s timeout covers Gemini Pro on large prompts (reflection,
            # dedup confirm with all candidate decisions expanded) and
            # absorbs occasional Gemini API slowness without cascading
            # failures. Background paths are fine waiting longer; there
            # are no interactive callers left.
            self._http = httpx.AsyncClient(base_url=DEJA_API_URL, timeout=300)

    @property
    def client(self):
        """Direct genai.Client — only available in direct mode (GEMINI_API_KEY set).
        Used by chat_routes.py for the tool-calling loop.
        """
        if not self._direct_client:
            raise RuntimeError(
                "GeminiClient.client requires GEMINI_API_KEY (direct mode). "
                "Use _generate() for proxy-compatible calls."
            )
        return self._direct_client

    async def _generate_full(self, model: str, contents, config_dict: dict) -> dict:
        """Like _generate but returns the full response dict (including
        function_call parts for tool-calling flows like chat).
        """
        if self._direct_client:
            from google.genai import types
            resp = await self._direct_client.aio.models.generate_content(
                model=model,
                contents=contents,
                config=types.GenerateContentConfig(**config_dict),
            )
            return resp

        token = get_auth_token()
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        serialized = self._serialize_contents(contents)
        resp = await self._http.post("/v1/generate", json={
            "model": model,
            "contents": serialized,
            "config": config_dict,
        }, headers=headers)
        resp.raise_for_status()
        return resp.json()

    async def _generate(self, model: str, contents, config_dict: dict) -> str:
        """Unified generate call: proxy or direct SDK.

        Every call gets a request_id for end-to-end tracing. The ID is
        sent to the server as a header and logged in telemetry, so
        errors can be correlated between client and server logs.
        """
        import time as _time
        from deja.telemetry import track_llm_call, new_request_id

        request_id = new_request_id()
        t0 = _time.time()
        try:
            if self._direct_client:
                from google.genai import types
                resp = await self._direct_client.aio.models.generate_content(
                    model=model,
                    contents=contents,
                    config=types.GenerateContentConfig(**config_dict),
                )
                duration = int((_time.time() - t0) * 1000)
                track_llm_call(model=model, duration_ms=duration, ok=True, request_id=request_id)
                return resp.text

            token = get_auth_token()
            headers = {
                "X-Request-ID": request_id,
            }
            if token:
                headers["Authorization"] = f"Bearer {token}"
            serialized = self._serialize_contents(contents)
            resp = await self._http.post("/v1/generate", json={
                "model": model,
                "contents": serialized,
                "config": config_dict,
            }, headers=headers)
            resp.raise_for_status()
            duration = int((_time.time() - t0) * 1000)
            track_llm_call(model=model, duration_ms=duration, ok=True, request_id=request_id)
            return resp.json()["text"]
        except Exception as e:
            duration = int((_time.time() - t0) * 1000)
            track_llm_call(model=model, duration_ms=duration, ok=False, error=type(e).__name__, request_id=request_id)
            # Attach request_id to the exception so callers can show it
            e.request_id = request_id  # type: ignore[attr-defined]
            raise

    @staticmethod
    def _serialize_contents(contents) -> Any:
        """Convert contents to a JSON-serializable form.

        Plain strings pass through. Lists may contain Part-like dicts
        with base64-encoded binary data for image/audio payloads.
        """
        if isinstance(contents, str):
            return contents
        if isinstance(contents, list):
            out = []
            for item in contents:
                if isinstance(item, str):
                    out.append(item)
                elif isinstance(item, dict):
                    out.append(item)
                else:
                    # Assume it's some object — pass as string
                    out.append(str(item))
            return out
        return contents

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
            from deja.observations.contacts import get_contacts_summary
            contacts_text = get_contacts_summary()
        except Exception:
            contacts_text = "(contacts unavailable)"

        from deja.wiki_schema import load_schema
        schema = load_schema()

        from deja.identity import load_user
        user_fields = load_user().as_prompt_fields()

        # Goals — standing instructions that shape what the agent prioritizes.
        from deja.config import WIKI_DIR
        goals_path = WIKI_DIR / "goals.md"
        goals_text = ""
        try:
            if goals_path.exists():
                goals_text = goals_path.read_text()
        except Exception:
            pass

        prompt = load_prompt("integrate").format(
            current_time=current_time,
            day_of_week=day_of_week,
            time_of_day=time_of_day,
            contacts_text=contacts_text,
            schema=schema,
            goals=goals_text or "(no goals.md)",
            wiki_text=wiki_text or "(empty)",
            signals_text=signals_text or "(no new signals)",
            **user_fields,
        )

        resp_text = await self._generate(
            model=INTEGRATE_MODEL,
            contents=prompt,
            config_dict={
                "response_mime_type": "application/json",
                "max_output_tokens": 16384,
                "temperature": 0.2,
            },
        )

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
            from deja.observations.contacts import get_contacts_summary
            contacts_text = get_contacts_summary()
        except Exception:
            contacts_text = "(contacts unavailable)"

        from deja.wiki_schema import load_schema
        schema = load_schema()

        from deja.identity import load_user
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

        resp_text = await self._generate(
            model=_ONBOARD_MODEL,
            contents=prompt,
            config_dict={
                "response_mime_type": "application/json",
                "max_output_tokens": 16384,
                "temperature": 0.2,
            },
        )

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
        self, image_path: str, goals_text: str = "", voice_context: str = ""
    ) -> dict:
        """Describe a screenshot via Gemini Flash vision.

        The prompt is grounded in the current wiki `index.md` so the
        model can name specific entities when they appear on screen
        ("this Zillow listing relates to [[palo-alto-relocation]]")
        instead of producing generic descriptions.

        If voice_context is provided (e.g. recent dictation), it's
        treated as the user's own commentary on what they're looking at
        and given top priority for grounding the description.

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
        from deja.llm.prefilter import load_index_md
        from deja.identity import load_user
        index_md = load_index_md().strip() or "(no wiki entries yet)"
        user_fields = load_user().as_prompt_fields()
        template = load_prompt("describe_screen")
        try:
            prompt = template.format(index_md=index_md, **user_fields)
        except KeyError:
            # Prompt doesn't use one of the expected placeholders — render
            # as-is rather than blowing up on a partial template.
            prompt = template

        # Voice dictation captured around this screenshot is the user's own
        # commentary on what they're looking at — prepend it as top-priority
        # context so the description matches their intent.
        if voice_context:
            prompt = (
                f"# IMPORTANT: The user just said this while looking at the screen\n\n"
                f'"{voice_context}"\n\n'
                f"Use their words as the primary lens for interpreting the screen. "
                f"What they said reveals their intent, what they're focused on, and "
                f"how they want this moment understood. Ground your description in "
                f"their commentary.\n\n"
                f"---\n\n{prompt}"
            )

        # Vision uses its own model (VISION_MODEL) chosen by the
        # tools/vision_eval.py harness — Flash in the default config
        # because it produces ~4x more wiki-link grounding than Flash-Lite
        # on real fixtures and outperforms Pro on every aggregate metric
        # at 1/4 the cost. Max tokens bumped to 1024 because Flash is
        # more verbose than Flash-Lite and 800 was truncating some frames.
        if self._direct_client:
            from google.genai import types
            contents = [
                types.Part.from_bytes(data=image_bytes, mime_type=mime),
                prompt,
            ]
        else:
            contents = [
                {"type": "bytes", "data": base64.b64encode(image_bytes).decode(), "mime_type": mime},
                prompt,
            ]

        resp_text = await self._generate(
            model=VISION_MODEL,
            contents=contents,
            config_dict={
                "max_output_tokens": 1024,
                "temperature": 0.2,
            },
        )

        if not resp_text:
            log.warning("Vision returned empty response (model=%s)", VISION_MODEL)
            return {"summary": "Screenshot analysis failed", "app": "", "key_details": ""}

        return {"summary": resp_text.strip()[:1000], "app": "", "key_details": ""}

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

        if self._direct_client:
            from google.genai import types
            contents = [
                types.Part.from_bytes(data=audio_bytes, mime_type="audio/wav"),
                prompt,
            ]
        else:
            contents = [
                {"type": "bytes", "data": base64.b64encode(audio_bytes).decode(), "mime_type": "audio/wav"},
                prompt,
            ]

        raw = await self._generate(
            model="gemini-2.5-flash",
            contents=contents,
            config_dict={
                "max_output_tokens": 2048,
                "temperature": 0.0,
            },
        )
        raw = raw or ""
        log.info("transcribe_audio raw response: %r", raw[:500])

        transcript = raw.strip()
        # Strip any stray quotation marks or "no speech" filler from the
        # handful of cases where the model ignores the prompt.
        if transcript.lower() in ("empty", "(empty)", "no speech", "no speech detected", ""):
            return ""
        return transcript.strip('"').strip("'").strip()
