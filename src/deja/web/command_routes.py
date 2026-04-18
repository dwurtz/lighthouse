"""Command endpoint — single-turn structured command dispatch.

POST /api/command with {"input": "..."} returns a typed response
describing what happened. One Flash-Lite call, one dispatch, no
conversation state, no tool loops. Replaces the old chat endpoint.

The classifier returns one of four types:
  - action: a one-off calendar/email/task/notify via goal_actions.execute_all
  - goal: a new tasks/waiting-for item in goals.md
  - automation: a new rule appended to the ## Automations section
  - context: a user_note observation + immediate integrate trigger

No silent fallbacks: any proxy failure, JSON parse failure, or unknown
command type raises a clear HTTPException so the UI can surface it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from deja.config import DEJA_HOME, QMD_COLLECTION, WIKI_DIR
from deja.identity import load_user
from deja.llm_client import GeminiClient
from deja.observability import DejaError, report_error, request_scope
from deja.prompts import load as load_prompt

# Retrieval budget for the command classifier. Keep this tight: the
# classifier is on the hot path for every typed/spoken command, and Flash-
# Lite latency matters more than recall here. We just need enough hits to
# disambiguate entities ("amanda" -> Amanda Peffer) and ground parameter
# extraction — not a full briefing.
_CLASSIFIER_RETRIEVAL_LIMIT = 5
# BM25 search is ~0.3s; we deliberately avoid ``qmd query`` here
# because its HyDE rerank issues an LLM call per search (~10s), which
# dwarfs the whole command latency budget. BM25 disambiguates named
# entities ("Amanda" → amanda-peffer.md) just fine — HyDE only helps
# with conceptual/fuzzy queries we don't need for command dispatch.
_CLASSIFIER_RETRIEVAL_TIMEOUT_S = 3.0

log = logging.getLogger(__name__)

router = APIRouter()

COMMAND_MODEL = "gemini-2.5-flash-lite"
FLASH_LITE_INPUT_PER_MTOK = 0.10
FLASH_LITE_OUTPUT_PER_MTOK = 0.40


class CommandRequest(BaseModel):
    input: str
    source: str = "text"  # "text" or "voice"


class CommandResponse(BaseModel):
    ok: bool
    type: str | None = None
    confirmation: str | None = None
    details: dict | None = None
    cost_usd: float | None = None
    latency_ms: int | None = None
    error: str | None = None


def _load_goals_text() -> str:
    """Read goals.md for classifier context. Raises if missing — every
    installed wiki has one via setup_api.py."""
    goals_path = WIKI_DIR / "goals.md"
    if not goals_path.exists():
        raise RuntimeError(
            f"goals.md not found at {goals_path}. Setup should have "
            f"created this file; re-run setup or investigate."
        )
    return goals_path.read_text(encoding="utf-8")


async def _retrieve_relevant_pages(input_text: str) -> str:
    """Run a BM25 search against the raw user input.

    Returns the formatted text output from ``qmd search``. This is
    explicitly BM25, not the hybrid ``qmd query`` path — see the
    ``_CLASSIFIER_RETRIEVAL_TIMEOUT_S`` comment for rationale.
    Raises ``RuntimeError`` on any failure so the classifier never
    runs blind (silent fallback was how "draft email to Amanda"
    previously failed — the model had no wiki context to resolve
    her email address).
    """
    topic = (input_text or "").strip()
    if not topic:
        return "(none)"

    def _run() -> str:
        import subprocess

        cmd = [
            "qmd", "search", topic,
            "-n", str(_CLASSIFIER_RETRIEVAL_LIMIT),
            "-c", QMD_COLLECTION,
        ]
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_CLASSIFIER_RETRIEVAL_TIMEOUT_S,
        )
        if r.returncode != 0:
            raise RuntimeError(
                f"qmd search failed (rc={r.returncode}): "
                f"{r.stderr[:400] or '(no stderr)'}"
            )
        return (r.stdout or "").strip()

    result = await asyncio.to_thread(_run)
    return result or "(none)"


async def _classify(input_text: str) -> tuple[dict, float, int]:
    """Call Flash-Lite classifier. Returns (parsed, cost_usd, latency_ms)."""
    template = load_prompt("command")
    user_fields = load_user().as_prompt_fields()
    relevant_pages = await _retrieve_relevant_pages(input_text)
    prompt = template.format(
        user_first_name=user_fields.get("user_first_name", "the user"),
        current_goals=_load_goals_text(),
        relevant_pages=relevant_pages,
        current_time_iso=datetime.now().astimezone().isoformat(),
        user_input=input_text,
    )

    client = GeminiClient()
    t0 = time.time()
    resp = await client._generate_full(
        model=COMMAND_MODEL,
        contents=prompt,
        config_dict={
            "response_mime_type": "application/json",
            "max_output_tokens": 2048,
            "temperature": 0.1,
        },
    )
    latency_ms = int((time.time() - t0) * 1000)

    # Extract raw text + usage metadata, same shape as dedup.py
    if isinstance(resp, dict):
        raw = resp.get("text") or ""
        um = resp.get("usage_metadata") or {}
        in_tok = int(um.get("prompt_token_count") or 0)
        out_tok = int(um.get("candidates_token_count") or 0) + int(
            um.get("thoughts_token_count") or 0
        )
    else:
        raw = getattr(resp, "text", "") or ""
        um = getattr(resp, "usage_metadata", None)
        in_tok = int(getattr(um, "prompt_token_count", 0) or 0) if um else 0
        cand = int(getattr(um, "candidates_token_count", 0) or 0) if um else 0
        thoughts = int(getattr(um, "thoughts_token_count", 0) or 0) if um else 0
        out_tok = cand + thoughts

    cost = (
        (in_tok / 1_000_000) * FLASH_LITE_INPUT_PER_MTOK
        + (out_tok / 1_000_000) * FLASH_LITE_OUTPUT_PER_MTOK
    )

    # Strip optional markdown fences just in case
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"Command classifier returned unparseable JSON: {e}. "
            f"Raw payload (first 1000 chars): {raw[:1000]!r}"
        ) from e

    return parsed, cost, latency_ms


# ---------------------------------------------------------------------------
# Dispatchers — one per command type
# ---------------------------------------------------------------------------


def _translate_action_params(action_type: str, params: dict) -> dict:
    """Map the classifier's *_iso param names to goal_actions.py's flat keys.

    goal_actions expects `start`/`end`/`due` (ISO strings), but the
    command prompt uses `start_iso`/`end_iso`/`due_iso` for clarity.
    If end_iso is missing on calendar_create, default to start + 15min.
    """
    p = dict(params or {})

    if action_type == "calendar_create":
        start_iso = p.pop("start_iso", None) or p.get("start")
        end_iso = p.pop("end_iso", None) or p.get("end")
        if not start_iso:
            raise HTTPException(
                400, "calendar_create payload missing start_iso"
            )
        if not end_iso:
            try:
                dt = datetime.fromisoformat(start_iso)
                end_iso = (dt + timedelta(minutes=15)).isoformat()
            except Exception as e:
                raise HTTPException(
                    400,
                    f"calendar_create: bad start_iso {start_iso!r}: {e}",
                ) from e
        p["start"] = start_iso
        p["end"] = end_iso

    elif action_type == "calendar_update":
        if "start_iso" in p:
            p["start"] = p.pop("start_iso")
        if "end_iso" in p:
            p["end"] = p.pop("end_iso")

    elif action_type == "create_task":
        if "due_iso" in p:
            p["due"] = p.pop("due_iso")

    elif action_type == "notify":
        # goal_actions._notify expects `title` and `message`
        if "body" in p and "message" not in p:
            p["message"] = p.pop("body")

    elif action_type == "draft_email":
        to = p.get("to")
        if isinstance(to, list):
            p["to"] = ", ".join(str(x) for x in to)
        # Validate here rather than silently falling through to
        # goal_actions._draft_email, which logs a warning and returns.
        # When classifier retrieval fails and the model can't resolve
        # the recipient, the user deserves an explicit error so they
        # know to re-try or disambiguate.
        if not p.get("to"):
            raise HTTPException(
                400,
                "draft_email: recipient unresolved — classifier had no "
                "wiki context for entity lookup. Retry the command or "
                "name the recipient explicitly.",
            )
        if not p.get("subject"):
            raise HTTPException(
                400, "draft_email: missing subject"
            )

    return p


def _dispatch_action(payload: dict) -> dict:
    """Execute a one-off action via goal_actions.execute_with_artifacts.

    Captures created artifacts (calendar event id, draft id, etc.) and
    registers them under a short-TTL undo token so the UI can offer an
    Undo button in the pill.
    """
    from deja.goal_actions import execute_with_artifacts

    action_type = payload.get("action_type")
    params = payload.get("params") or {}
    if not action_type:
        raise HTTPException(400, "Action payload missing action_type")

    translated = _translate_action_params(action_type, params)
    executed, artifacts = execute_with_artifacts(
        [{"type": action_type, "params": translated, "reason": "command"}]
    )
    details: dict = {
        "executed": executed,
        "action_type": action_type,
        "params": translated,
    }
    if artifacts:
        details["undo_token"] = _register_undo(artifacts)
    return details


# ---------------------------------------------------------------------------
# Undo registry — voice UX shows an Undo button for UNDO_TTL_SEC after
# an action dispatches. The token maps to the list of artifacts we
# captured during dispatch; the undo endpoint reverses each.
# ---------------------------------------------------------------------------

UNDO_TTL_SEC = 15  # slightly larger than the UI's 5s so network round-trip fits


_undo_registry: dict[str, tuple[float, list[dict]]] = {}


def _register_undo(artifacts: list[dict]) -> str:
    """Store artifacts under a short opaque token. Returns the token."""
    import secrets
    token = secrets.token_urlsafe(12)
    _undo_registry[token] = (time.time() + UNDO_TTL_SEC, artifacts)
    _prune_undo_registry()
    return token


def _prune_undo_registry() -> None:
    now = time.time()
    expired = [t for t, (exp, _) in _undo_registry.items() if exp < now]
    for t in expired:
        _undo_registry.pop(t, None)


def _undo_artifact(artifact: dict) -> dict:
    """Reverse a single artifact. Returns a result dict for logging."""
    kind = artifact.get("kind", "")
    if kind == "calendar_event":
        from deja.goal_actions import _service
        svc = _service("calendar", "v3")
        if svc is None:
            return {"kind": kind, "ok": False, "reason": "no calendar service"}
        event_id = artifact.get("id", "")
        try:
            svc.events().delete(
                calendarId="primary", eventId=event_id,
            ).execute()
            return {"kind": kind, "ok": True, "id": event_id}
        except Exception as e:
            return {
                "kind": kind, "ok": False,
                "reason": f"{type(e).__name__}: {e}",
            }
    if kind == "goal_line":
        from deja.goals import apply_tasks_update
        section = artifact.get("section", "tasks")
        text = artifact.get("text", "")
        archive_key = (
            "archive_tasks" if section == "tasks" else "archive_waiting"
        )
        try:
            applied = apply_tasks_update({
                archive_key: [{"needle": text, "reason": "undo via voice UX"}],
            })
            return {"kind": kind, "ok": applied > 0, "text": text[:60]}
        except Exception as e:
            return {
                "kind": kind, "ok": False,
                "reason": f"{type(e).__name__}: {e}",
            }
    return {"kind": kind, "ok": False, "reason": "unsupported artifact kind"}


@router.post("/api/command/undo/{token}")
def command_undo(token: str) -> dict:
    """Reverse the action identified by ``token``. Returns per-artifact
    results. Token is consumed on first use.
    """
    _prune_undo_registry()
    entry = _undo_registry.pop(token, None)
    if entry is None:
        raise HTTPException(404, "Undo token expired or unknown")
    _, artifacts = entry
    results = [_undo_artifact(a) for a in artifacts]
    try:
        from deja import audit
        audit.record(
            "voice_undo",
            target=f"command/undo/{token}",
            reason=f"undid {sum(1 for r in results if r.get('ok'))}/{len(results)}",
        )
    except Exception:
        pass
    return {"ok": True, "results": results}


def _dispatch_goal(payload: dict) -> dict:
    """Add a new task or waiting-for item to goals.md.

    Registers an undo token so the voice UX can archive the item if the
    classifier misfired.
    """
    from deja.goals import apply_tasks_update

    section = (payload.get("section") or "tasks").strip()
    text = (payload.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "Goal payload missing text")

    key = "add_tasks" if section == "tasks" else "add_waiting"
    changes = apply_tasks_update({key: [text]})
    details: dict = {"section": section, "text": text, "applied": changes}
    if changes > 0:
        details["undo_token"] = _register_undo([{
            "kind": "goal_line",
            "section": section,
            "text": text,
        }])
    return details


def _dispatch_automation(payload: dict) -> dict:
    """Append a rule to goals.md ## Automations section."""
    from deja.goals import append_to_automations_section

    text = (payload.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "Automation payload missing text")

    try:
        append_to_automations_section(text)
    except RuntimeError as e:
        raise HTTPException(500, str(e)) from e
    return {"automation": text}


async def _dispatch_query(payload: dict) -> dict:
    """Answer a user question by synthesizing across wiki + goals + activity.

    The command classifier tags a question as ``query`` with a ``topic``
    hint. We assemble a bundle of (a) wiki context for that topic via
    the same retrieval used by MCP get_context, (b) the goals.md slice
    touching the topic, and (c) the last ~hour of observations. Then
    Flash-Lite turns it into a short direct answer following the
    ``query.md`` prompt.

    Returns ``{"answer": "...", "topic": "..."}`` — the Swift chat view
    renders ``answer`` as markdown directly.
    """
    from deja import audit
    from deja.llm_client import GeminiClient
    from deja.prompts import load as load_prompt
    from deja.identity import load_user

    question = (payload.get("question") or "").strip()
    topic = (payload.get("topic") or "").strip() or question

    if not question:
        raise HTTPException(400, "Query payload missing question")

    # Assemble the same three-part bundle MCP get_context returns, but
    # without the user profile header (we pass that separately in the
    # prompt) and with a shorter observation window.
    try:
        from deja.mcp_server import _goals_for_topic, _qmd_query
    except Exception:
        log.exception("query: failed to import mcp helpers")
        raise HTTPException(500, "Query synthesis unavailable") from None

    parts: list[str] = []

    if topic and topic != "*":
        from deja.config import QMD_COLLECTION
        qmd_text = _qmd_query(topic, collection=QMD_COLLECTION, limit=6)
        if qmd_text:
            parts.append(f"## Wiki pages relevant to \"{topic}\"\n\n{qmd_text}")

    goals_slice = _goals_for_topic(topic if topic != "*" else question)
    if goals_slice:
        parts.append(f"## Open commitments\n\n{goals_slice}")
    else:
        # Global query ("what's on my plate"): include the full briefing.
        from deja.briefing import build_briefing
        brief = build_briefing()
        if any(brief["counts"].values()):
            import json as _json
            parts.append(
                "## Full right-now briefing (no specific topic)\n\n"
                + "```json\n"
                + _json.dumps(brief, indent=2)
                + "\n```"
            )

    bundle = "\n\n---\n\n".join(parts) if parts else "(no relevant context found)"

    user = load_user()
    prompt = load_prompt("query").format(
        user_first_name=user.first_name or user.name or "the user",
        user_profile=(user.profile_md or "").strip() or "(no profile yet)",
        question=question,
        bundle=bundle,
    )

    client = GeminiClient()
    try:
        answer = await client._generate(
            model="gemini-2.5-flash-lite",
            contents=prompt,
            config_dict={
                "max_output_tokens": 2048,
                "temperature": 0.3,
            },
        )
    except Exception as e:
        log.exception("query: Flash-Lite call failed")
        raise HTTPException(500, f"Synthesis failed: {e}") from e

    answer = (answer or "").strip()
    if not answer:
        answer = "(no answer returned)"

    audit.record(
        "user_command",
        target=f"query/{topic}"[:120],
        reason=question[:200],
        trigger={"kind": "user_cmd", "detail": "query"},
    )

    return {"answer": answer, "topic": topic, "bundle_chars": len(bundle)}


def _dispatch_context(payload: dict) -> dict:
    """Append a user_note observation to observations.jsonl."""
    obs_path = DEJA_HOME / "observations.jsonl"
    obs_path.parent.mkdir(parents=True, exist_ok=True)

    text = (payload.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "Context payload missing text")

    ts = datetime.now().astimezone().isoformat()
    priority = payload.get("priority", "normal") or "normal"
    entry = {
        "source": "user_note",
        "sender": "You",
        "text": text,
        "timestamp": ts,
        "priority": priority,
        "id_key": f"user_note:{ts}",
    }
    with obs_path.open("a") as f:
        f.write(json.dumps(entry) + "\n")

    try:
        from deja import audit

        audit.record(
            "user_command",
            target="command/context",
            reason=text[:200],
            trigger={"kind": "user_cmd", "detail": "context dispatch"},
        )
    except Exception:
        log.debug("audit.record failed for context", exc_info=True)

    return {"observation_id": entry["id_key"], "priority": priority}


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.post("/api/command")
async def handle_command(body: CommandRequest):
    """Classify and dispatch a user command. Single turn, no history.

    Wrapped in a ``request_scope`` so every log line, audit entry, and
    LLM call made while handling this request shares one correlation
    id. Any :class:`DejaError` bubbling up is reported to both error
    sinks (visible to the user) and returned as a JSON 500 so the UI
    can surface the typed code instead of a raw stack trace.
    """
    with request_scope() as req_id:
        try:
            return await _handle_command_inner(body, req_id)
        except DejaError as err:
            report_error(err, visible_to_user=True)
            return JSONResponse(
                status_code=500,
                content={
                    "ok": False,
                    "request_id": req_id,
                    "code": err.code,
                    "error": err.user_message,
                },
            )


async def _handle_command_inner(
    body: "CommandRequest", req_id: str
) -> CommandResponse:
    log.info(
        "command received (source=%s, chars=%d)", body.source, len(body.input)
    )

    if not (body.input or "").strip():
        raise HTTPException(400, "Command input is empty")

    try:
        parsed, cost, latency_ms = await _classify(body.input)
    except HTTPException:
        raise
    except Exception as e:
        log.exception("command classifier failed")
        raise HTTPException(500, f"Classifier failed: {e}") from e

    cmd_type = parsed.get("type")
    payload = parsed.get("payload") or {}
    confirmation = parsed.get("confirmation", "") or ""

    if cmd_type == "action":
        details = _dispatch_action(payload)
    elif cmd_type == "goal":
        details = _dispatch_goal(payload)
    elif cmd_type == "automation":
        details = _dispatch_automation(payload)
    elif cmd_type == "context":
        details = _dispatch_context(payload)
    elif cmd_type == "query":
        details = await _dispatch_query(payload)
        # Overwrite the classifier's placeholder confirmation with the
        # synthesized answer so the chat surface shows the actual reply.
        confirmation = details.get("answer", confirmation)
    else:
        raise HTTPException(400, f"Unknown command type: {cmd_type!r}")

    # Record every dispatched command in the audit log.
    try:
        from deja import audit

        audit.record(
            "user_command",
            target=f"command/{cmd_type}",
            reason=confirmation or body.input[:200],
            trigger={"kind": "user_cmd", "detail": body.source or "command"},
        )
    except Exception:
        log.debug("audit.record failed for command", exc_info=True)

    # Business-intelligence telemetry — proxy sees cmd type + source
    # (voice vs typed) + input length. No input content.
    try:
        from deja.telemetry import track

        track("command_dispatched", {
            "cmd_type": cmd_type or "unknown",
            "source": body.source or "command",
            "input_chars": len(body.input or ""),
            "latency_ms": latency_ms,
            "cost_usd": round(cost, 6),
        })
    except Exception:
        log.debug("command telemetry failed", exc_info=True)

    # Context-type commands fire an immediate integrate trigger with
    # cross-modal batch merging so the new signal gets processed against
    # any other unprocessed signals on the current cycle.
    if cmd_type == "context":
        from deja.agent.analysis_cycle import trigger_integrate_now

        try:
            asyncio.create_task(trigger_integrate_now(reason="command_context"))
        except Exception:
            log.debug(
                "immediate integrate trigger failed for command_context",
                exc_info=True,
            )

    return CommandResponse(
        ok=True,
        type=cmd_type,
        confirmation=confirmation,
        details=details,
        cost_usd=round(cost, 6),
        latency_ms=latency_ms,
    )
