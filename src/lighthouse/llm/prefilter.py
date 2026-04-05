"""Signal triage via Gemini Flash-Lite.

One batched call per cycle filters all candidate signals at once. This used
to be a local llama.cpp integration; benchmarks showed Flash-Lite was ~100x
faster and more accurate than any local Gemma variant, so the local path was
removed. The module name is vestigial — it still hosts the triage contract,
just routed through the cloud.

A single cycle typically has 5-30 message-type signals. Instead of firing
N parallel calls (N × wiki-index tokens, N × HTTP overhead), we build one
prompt containing all candidates and ask Flash-Lite to return one verdict
per input. The wiki index is transmitted exactly once per cycle.

Recall-biased: on any failure — API error, JSON parse error, mismatched
verdict count — every signal is kept. We'd rather burn a few cycle tokens
than silently lose real context.
"""

from __future__ import annotations

import json
import logging

log = logging.getLogger(__name__)


# Sources that get triaged before reaching the main analysis cycle.
# Everything else (calendar, drive, tasks, screenshot, clipboard, microphone)
# passes through unfiltered — those sources are already low-volume and curated
# by upstream logic (e.g. microphone transcripts are user-initiated).
TRIAGE_SOURCES = {"imessage", "whatsapp", "email", "browser"}


def _load_index_md() -> str:
    """Read the current wiki index for triage grounding. Empty on miss."""
    from lighthouse.config import WIKI_DIR
    path = WIKI_DIR / "index.md"
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def load_index_md() -> str:
    """Back-compat alias for callers that still import the old name."""
    return _load_index_md()


def _format_signals_block(items: list[dict]) -> str:
    """Render a numbered list of signal dicts for the batched triage prompt.

    Each line looks like:
        1. [imessage] Justin (Molly's Dad): "We're going to stay too..."
    """
    lines: list[str] = []
    for idx, d in enumerate(items, start=1):
        source = d.get("source", "?")
        sender = (d.get("sender") or "?").replace("\n", " ")[:80]
        text = (d.get("text") or "").replace("\n", " ").strip()[:600]
        lines.append(f"{idx}. [{source}] {sender}: {text}")
    return "\n".join(lines)


async def triage_batch(
    items: list[dict],
    *,
    index_md: str | None = None,
) -> list[tuple[bool, str]]:
    """Triage a batch of signals in a single Flash-Lite call.

    Takes a list of signal dicts (each with source/sender/text fields) and
    returns a list of ``(relevant, reason)`` tuples in the same order.

    Recall-biased: on any failure (API error, JSON parse error, malformed
    response, wrong verdict count) every signal in the batch is kept.
    """
    if not items:
        return []

    from lighthouse.config import INTEGRATE_MODEL
    from lighthouse.llm_client import GeminiClient, types
    from lighthouse.prompts import load as load_prompt

    if index_md is None:
        index_md = _load_index_md()
    index_block = index_md.strip() or "(no wiki entries yet)"

    try:
        template = load_prompt("prefilter")
    except FileNotFoundError:
        log.warning(
            "prefilter prompt missing at Lighthouse/prompts/prefilter.md"
        )
        return [(True, "triage prompt missing — keeping")] * len(items)

    signals_block = _format_signals_block(items)
    from lighthouse.identity import load_user
    user_fields = load_user().as_prompt_fields()
    try:
        prompt = template.format(
            index_md=index_block,
            signals_block=signals_block,
            **user_fields,
        )
    except KeyError as e:
        log.warning("prefilter prompt missing placeholder %s", e)
        return [(True, "triage template error — keeping")] * len(items)

    gemini = GeminiClient()
    try:
        resp = await gemini.client.aio.models.generate_content(
            model=INTEGRATE_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                max_output_tokens=2048,
                temperature=0.1,
            ),
        )
        raw = resp.text or ""
    except Exception as e:
        log.warning("triage batch Flash-Lite call failed: %s", e)
        return [(True, "triage API failed — keeping")] * len(items)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                data = json.loads(raw[start:end + 1])
            except json.JSONDecodeError:
                log.warning("triage batch parse failed: %s", raw[:200])
                return [(True, "triage parse failed — keeping")] * len(items)
        else:
            return [(True, "triage parse failed — keeping")] * len(items)

    if not isinstance(data, dict) or "verdicts" not in data:
        log.warning("triage batch response has no verdicts: %s", str(data)[:200])
        return [(True, "triage shape error — keeping")] * len(items)

    verdicts = data.get("verdicts") or []
    if not isinstance(verdicts, list):
        return [(True, "triage verdicts not a list — keeping")] * len(items)

    # Build a result list indexed by position. Missing verdicts default to
    # keep. Extra verdicts from the model are ignored.
    result: list[tuple[bool, str]] = [(True, "triage missing verdict — keeping")] * len(items)
    for v in verdicts:
        if not isinstance(v, dict):
            continue
        idx = v.get("id")
        if not isinstance(idx, int) or idx < 1 or idx > len(items):
            continue
        relevant = bool(v.get("relevant", True))
        reason = str(v.get("reason", ""))[:120]
        result[idx - 1] = (relevant, reason)

    return result
