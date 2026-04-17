"""Gemini LLM client for Déjà.

Routes all LLM calls through the Deja API server proxy. When the
``GEMINI_API_KEY`` env var is set, falls back to direct google-genai
SDK calls (developer / CI mode).

The server proxy is at ``DEJA_API_URL`` (default ``https://api.trydeja.com``)
and accepts ``POST /v1/generate`` with ``Authorization: Bearer <token>``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from google.genai import types  # re-exported for reflection.py and prefilter.py

from deja.auth import get_auth_token
from deja.config import INTEGRATE_MODEL, REFLECT_MODEL, VISION_MODEL

DEJA_API_URL = os.environ.get("DEJA_API_URL", "https://deja-api.onrender.com")
_USE_DIRECT = bool(os.environ.get("GEMINI_API_KEY"))

# Hard ceiling on the goals.md text injected into integrate's prompt.
# goals.py enforces section caps and auto-expiry as the primary bounds;
# this is the format-time safety net — if a wiki file drifted past the
# caps (e.g. user manually added hundreds of tasks), we still cap what
# lands in the prompt so the per-cycle token bill can't explode.
# 6000 chars ≈ 1500 tokens. Trimming drops oldest Reminders first,
# then oldest Waiting for, then oldest Tasks. The preamble and Archive
# are never dropped (Archive is already capped at 100 by goals.py).
_GOALS_MAX_CHARS = 6000

# Onboarding is a one-time, high-stakes run against potentially the
# user's entire digital history. Cost is amortized across the one run,
# so we use the strongest available model for maximum quality rather
# than the steady-state Flash-Lite used by the every-few-minutes cycle.
_ONBOARD_MODEL = REFLECT_MODEL
from deja.prompts import load as load_prompt

log = logging.getLogger(__name__)


def _truncate_goals_text(text: str) -> str:
    """Drop oldest items from Reminders → Waiting for → Tasks until under cap.

    Only fires when ``text`` exceeds ``_GOALS_MAX_CHARS``. goals.py
    enforces the real caps; this is the format-time safety net.
    """
    if not text or len(text) <= _GOALS_MAX_CHARS:
        return text
    try:
        from deja.goals import _parse_sections, _render_sections
    except Exception:
        return text[:_GOALS_MAX_CHARS]

    preamble, sections = _parse_sections(text)

    def _drop_oldest_bullet(section_name: str) -> bool:
        lines = sections.get(section_name, [])
        for i, ln in enumerate(lines):
            if ln.lstrip().startswith("- "):
                lines.pop(i)
                return True
        return False

    drop_order = ["Reminders", "Waiting for", "Tasks"]
    dropped = 0
    while True:
        rendered = _render_sections(preamble, sections)
        if len(rendered) <= _GOALS_MAX_CHARS:
            break
        made_progress = False
        for name in drop_order:
            if _drop_oldest_bullet(name):
                dropped += 1
                made_progress = True
                break
        if not made_progress:
            rendered = rendered[:_GOALS_MAX_CHARS]
            break

    if dropped:
        log.warning(
            "goals truncation: dropped %d oldest bullet(s) to fit %d-char cap",
            dropped,
            _GOALS_MAX_CHARS,
        )
    return rendered


def _normalize_wiki_update(upd: dict) -> dict:
    """Bring one wiki_update dict into the new schema.

    The new integrate contract (2026-04-16) is:

        {category, slug, action, body_markdown, event_metadata?, reason}

    Old integrate responses (bundled apps, onboarding, contradictions)
    emit ``content`` instead — a full markdown blob that may start with
    a YAML frontmatter block. Translate those here so downstream sees
    one shape, while leaving the original keys intact as a fallback.

    Rules:
      * If ``body_markdown`` is already set, leave the dict alone
        (already new-shape) but opportunistically drop a leading
        frontmatter block the model slipped in.
      * Else read ``content``. For events, pull the YAML block into
        ``event_metadata`` (best-effort) and put the remainder into
        ``body_markdown``. For people/projects, strip any leading YAML
        block — frontmatter ownership moved to the write path.

    Never raises on malformed shapes. Unknown categories pass through
    unchanged (apply_updates' validator will reject them).
    """
    # Already new-shape.
    if isinstance(upd.get("body_markdown"), str):
        try:
            from deja.wiki import _strip_leading_frontmatter
            upd["body_markdown"] = _strip_leading_frontmatter(upd["body_markdown"])
        except Exception:
            pass
        return upd

    content = upd.get("content")
    if not isinstance(content, str) or not content:
        return upd

    category = upd.get("category")

    if category == "events" and not isinstance(upd.get("event_metadata"), dict):
        # Best-effort YAML extraction from the legacy `content` field.
        try:
            import yaml as _yaml
            from deja.wiki import canonicalize_frontmatter, extract_frontmatter
            repaired, _ = canonicalize_frontmatter(content)
            fm_block, body = extract_frontmatter(repaired)
            if fm_block:
                inner = "\n".join(
                    ln for ln in fm_block.splitlines()
                    if ln.strip() != "---"
                ).strip()
                try:
                    parsed = _yaml.safe_load(inner) or {}
                    if isinstance(parsed, dict):
                        upd["event_metadata"] = parsed
                except _yaml.YAMLError:
                    pass
                upd["body_markdown"] = body
                return upd
        except Exception:
            pass
        upd["body_markdown"] = content
        return upd

    # people / projects: strip any leading frontmatter block the model
    # slipped in; the write path owns YAML for these pages now.
    try:
        from deja.wiki import _strip_leading_frontmatter
        upd["body_markdown"] = _strip_leading_frontmatter(content)
    except Exception:
        upd["body_markdown"] = content
    return upd


def _parse_json(raw: str) -> Any:
    """Best-effort JSON extraction from LLM output.

    All ``json.loads`` calls use ``strict=False`` so unescaped control
    characters (bare ``\\n`` / ``\\t``) inside string values don't
    crash the parse — Gemini occasionally emits these in narrative
    fields and there's no value in failing a whole cycle over one
    missing backslash.
    """
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
                return json.loads(text[start:end], strict=False)
            except json.JSONDecodeError:
                continue
    return json.loads(text, strict=False)


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

        httpx failures are translated into typed :class:`DejaError`
        subclasses so upstream catch sites (command_routes, mic_routes,
        analysis_cycle) can decide uniformly whether to surface the
        error to the user.
        """
        import time as _time
        from deja.telemetry import track_llm_call
        from deja.observability import (
            AuthError,
            LLMError,
            ProxyUnavailable,
            RateLimitError,
            current_request_id,
            new_request_id,
        )

        # Reuse an outer request_id if one is already bound (e.g. the
        # request_scope opened by command_routes / analysis_cycle) so the
        # whole call tree shares one id. Mint one only if we're called
        # outside any scope — then every LLM call is still traceable.
        request_id = current_request_id() or new_request_id()
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
            try:
                resp = await self._http.post("/v1/generate", json={
                    "model": model,
                    "contents": serialized,
                    "config": config_dict,
                }, headers=headers)
            except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as e:
                raise ProxyUnavailable(
                    f"proxy request failed: {type(e).__name__}: {e}",
                    details={"url": f"{DEJA_API_URL}/v1/generate", "model": model},
                ) from e

            status = resp.status_code
            if status >= 400:
                detail_body = ""
                try:
                    detail_body = resp.text[:500]
                except Exception:
                    pass
                details = {
                    "http_status": status,
                    "url": f"{DEJA_API_URL}/v1/generate",
                    "model": model,
                    "body": detail_body,
                }
                if status in (502, 503, 504):
                    raise ProxyUnavailable(
                        f"proxy returned {status}", details=details,
                    )
                if status in (401, 403):
                    raise AuthError(
                        f"proxy auth failed: {status}", details=details,
                    )
                if status == 429:
                    raise RateLimitError(
                        "proxy rate limited (429)", details=details,
                    )
                if 500 <= status < 600:
                    raise LLMError(
                        f"upstream llm returned {status}", details=details,
                    )
                # Other 4xx — surface as a generic LLMError so callers
                # can still report it without leaking raw httpx errors.
                raise LLMError(
                    f"proxy returned {status}", details=details,
                )

            duration = int((_time.time() - t0) * 1000)
            track_llm_call(model=model, duration_ms=duration, ok=True, request_id=request_id)
            return resp.json()["text"]
        except Exception as e:
            duration = int((_time.time() - t0) * 1000)
            track_llm_call(model=model, duration_ms=duration, ok=False, error=type(e).__name__, request_id=request_id)
            # Attach request_id to the exception so callers can show it
            try:
                e.request_id = request_id  # type: ignore[attr-defined]
            except Exception:
                pass
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
        open_windows: str = "",
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

        from deja.identity import load_user
        user_fields = load_user().as_prompt_fields()

        # Goals — standing instructions that shape what the agent prioritizes.
        # Safety-net truncated to _GOALS_MAX_CHARS so a runaway Reminders /
        # Waiting for section can't silently blow up the per-cycle token
        # bill. goals.py auto-expiry + caps are the primary bounds; this
        # is the last line of defense.
        from deja.config import WIKI_DIR
        goals_path = WIKI_DIR / "goals.md"
        goals_text = ""
        try:
            if goals_path.exists():
                goals_text = goals_path.read_text()
        except Exception:
            pass
        goals_text = _truncate_goals_text(goals_text)

        prompt = load_prompt("integrate").format(
            current_time=current_time,
            day_of_week=day_of_week,
            time_of_day=time_of_day,
            contacts_text=contacts_text,
            goals=goals_text or "(no goals.md)",
            wiki_text=wiki_text or "(empty)",
            signals_text=signals_text or "(no new signals)",
            open_windows=open_windows or "(not available)",
            **user_fields,
        )

        # Integrate shadow eval — when enabled, fires the same prompt
        # through an alternate model in parallel with the production
        # call. Only the production model's output drives the wiki;
        # the shadow output is serialized to
        # ``~/.deja/integrate_shadow/<ts>.json`` for offline comparison.
        #
        # The shadow model is the OTHER one — if production is Flash,
        # shadow is Flash-Lite (watching for "did we miss anything?").
        # If production is Flash-Lite, shadow is Flash (watching for
        # "did Flash catch something FL hallucinated?"). Either way,
        # we always have Flash-vs-Flash-Lite data.
        try:
            from deja.config import INTEGRATE_SHADOW_EVAL

            shadow_on = bool(INTEGRATE_SHADOW_EVAL)
        except Exception:
            shadow_on = False

        # Shadow set: the OTHER Flash variant (cheap A/B watchdog) plus
        # Gemini 3.1 Pro (expensive, sees if a stronger model would make
        # materially different calls). Order is preserved in the saved
        # record as `shadows: [...]`.
        shadow_models: list[str] = []
        if shadow_on:
            if INTEGRATE_MODEL == "gemini-2.5-flash":
                shadow_models.append("gemini-2.5-flash-lite")
            elif INTEGRATE_MODEL == "gemini-2.5-flash-lite":
                shadow_models.append("gemini-2.5-flash")
            shadow_models.append("gemini-3.1-pro-preview")

        shadow_tasks: list[tuple[str, "asyncio.Task[str]"]] = []
        for m in shadow_models:
            t = asyncio.create_task(
                self._generate(
                    model=m,
                    contents=prompt,
                    config_dict={
                        "response_mime_type": "application/json",
                        "max_output_tokens": 16384,
                        "temperature": 0.2,
                    },
                )
            )
            shadow_tasks.append((m, t))

        t0 = time.time()
        resp_text = await self._generate(
            model=INTEGRATE_MODEL,
            contents=prompt,
            config_dict={
                "response_mime_type": "application/json",
                "max_output_tokens": 16384,
                "temperature": 0.2,
            },
        )
        prod_latency_ms = int((time.time() - t0) * 1000)

        try:
            result = json.loads(resp_text, strict=False)
        except (json.JSONDecodeError, ValueError):
            result = _parse_json(resp_text)

        if not isinstance(result, dict):
            raise TypeError(f"integrate_observations expected dict, got {type(result).__name__}: {resp_text[:200]}")

        result.setdefault("reasoning", "")
        result.setdefault("wiki_updates", [])
        result.setdefault("observation_narrative", "")

        # Normalize wiki_updates to the new body_markdown + event_metadata
        # shape. Old bundled apps and onboarding still emit `content`;
        # wiki.apply_updates also accepts that, but normalizing here makes
        # downstream code (shadow-eval record, any new consumer) see a
        # consistent schema. Passthrough when already normalized.
        result["wiki_updates"] = [
            _normalize_wiki_update(u) for u in (result.get("wiki_updates") or [])
            if isinstance(u, dict)
        ]

        # Save shadow comparison after the production call completes —
        # never block the real cycle on the shadow. If the shadow task
        # fails or times out we just skip serialization; the production
        # path is unaffected.
        if shadow_tasks:
            try:
                await self._save_integrate_shadow(
                    prompt=prompt,
                    signals_text=signals_text,
                    prod_model=INTEGRATE_MODEL,
                    prod_result=result,
                    prod_latency_ms=prod_latency_ms,
                    shadow_tasks=shadow_tasks,
                )
            except Exception:
                log.debug("integrate shadow eval: save failed", exc_info=True)

        return result

    async def _save_integrate_shadow(
        self,
        prompt: str,
        signals_text: str,
        prod_model: str,
        prod_result: dict,
        prod_latency_ms: int,
        shadow_tasks: list,
    ) -> None:
        """Wait for all shadow calls and write a side-by-side JSON file.

        Called after the production result is already returned to the
        caller, so this runs in the tail of the cycle and never delays
        the real work. Each shadow's failure/timeout is independent —
        one slow model doesn't suppress the others.

        Record shape:
            production: {model, reasoning, wiki_updates, ...}
            shadows: [{model, reasoning, ...}, ...]   # ordered
        Legacy single-shadow readers can fall back to ``shadows[0]``.
        """
        async def _resolve(model: str, task) -> tuple[dict | None, int, str | None]:
            start = time.time()
            try:
                text = await asyncio.wait_for(task, timeout=45.0)
                latency = int((time.time() - start) * 1000)
                try:
                    parsed = json.loads(text, strict=False)
                except Exception:
                    parsed = _parse_json(text)
                if not isinstance(parsed, dict):
                    parsed = {"reasoning": "(non-dict response)", "raw": str(text)[:500]}
                parsed.setdefault("reasoning", "")
                parsed.setdefault("wiki_updates", [])
                return parsed, latency, None
            except asyncio.TimeoutError:
                return None, int((time.time() - start) * 1000), "timeout"
            except Exception as e:
                return None, int((time.time() - start) * 1000), f"{type(e).__name__}: {e}"[:200]

        results = await asyncio.gather(
            *[_resolve(m, t) for (m, t) in shadow_tasks],
            return_exceptions=False,
        )

        shadow_records = []
        for (model, _task), (parsed, latency, err) in zip(shadow_tasks, results):
            if parsed is None:
                shadow_records.append({"model": model, "error": err, "latency_ms": latency})
            else:
                shadow_records.append({
                    "model": model,
                    "reasoning": parsed.get("reasoning", ""),
                    "wiki_updates": parsed.get("wiki_updates", []),
                    "goal_actions": parsed.get("goal_actions", []),
                    "tasks_update": parsed.get("tasks_update", {}),
                    "latency_ms": latency,
                })

        from deja.config import DEJA_HOME
        shadow_dir = DEJA_HOME / "integrate_shadow"
        shadow_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        out_path = shadow_dir / f"{ts}.json"

        record = {
            "timestamp": datetime.now().isoformat(),
            "signals_text": signals_text[:20000],
            "prompt_tokens_approx": len(prompt) // 4,
            "production": {
                "model": prod_model,
                "reasoning": prod_result.get("reasoning", ""),
                "wiki_updates": prod_result.get("wiki_updates", []),
                "goal_actions": prod_result.get("goal_actions", []),
                "tasks_update": prod_result.get("tasks_update", {}),
                "latency_ms": prod_latency_ms,
            },
            "shadows": shadow_records,
        }
        try:
            out_path.write_text(json.dumps(record, indent=2, default=str))
            summary = ", ".join(
                f"{s['model']}→{len(s.get('wiki_updates', [])) if 'error' not in s else 'err'}"
                for s in shadow_records
            )
            log.info(
                "integrate shadow: saved %s (prod=%s→%d updates; shadows: %s)",
                out_path.name,
                prod_model,
                len(prod_result.get("wiki_updates", [])),
                summary,
            )
        except Exception:
            log.debug("integrate shadow: write failed", exc_info=True)

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

        from deja.identity import load_user
        user_fields = load_user().as_prompt_fields()

        prompt = load_prompt("onboard").format(
            current_time=current_time,
            day_of_week=day_of_week,
            time_of_day=time_of_day,
            contacts_text=contacts_text,
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
            result = json.loads(resp_text, strict=False)
        except (json.JSONDecodeError, ValueError):
            result = _parse_json(resp_text)

        if not isinstance(result, dict):
            raise TypeError(
                f"onboard_from_observations expected dict, got "
                f"{type(result).__name__}: {resp_text[:200]}"
            )

        result.setdefault("reasoning", "")
        result.setdefault("wiki_updates", [])
        # Onboarding prompt still uses the old `content` shape — normalize
        # so downstream code sees one schema. apply_updates accepts both,
        # but this keeps the on-disk shadow-eval / debug artifacts clean.
        result["wiki_updates"] = [
            _normalize_wiki_update(u) for u in (result.get("wiki_updates") or [])
            if isinstance(u, dict)
        ]
        return result

    # Screenshot analysis removed — OCR'd locally via the ``deja-ocr``
    # Swift binary (no LLM round-trip). The former cloud describe_screen
    # method and its shadow-eval harness were retired 2026-04-13 once
    # OCR proved sufficient for downstream integrate quality.
    _removed_describe_screen = True  # type: ignore[unused-ignore]
    async def _describe_screen_deleted(self, *a, **kw):  # pragma: no cover
        raise NotImplementedError(
            "describe_screen was removed; screenshots are OCR'd by deja-ocr"
        )

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
