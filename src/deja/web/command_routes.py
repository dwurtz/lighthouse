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
from pydantic import BaseModel

from deja.config import DEJA_HOME, WIKI_DIR
from deja.identity import load_user
from deja.llm_client import GeminiClient
from deja.prompts import load as load_prompt

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


async def _classify(input_text: str) -> tuple[dict, float, int]:
    """Call Flash-Lite classifier. Returns (parsed, cost_usd, latency_ms)."""
    template = load_prompt("command")
    user_fields = load_user().as_prompt_fields()
    prompt = template.format(
        user_first_name=user_fields.get("user_first_name", "the user"),
        current_goals=_load_goals_text(),
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

    return p


def _dispatch_action(payload: dict) -> dict:
    """Execute a one-off action via goal_actions.execute_all."""
    from deja.goal_actions import execute_all

    action_type = payload.get("action_type")
    params = payload.get("params") or {}
    if not action_type:
        raise HTTPException(400, "Action payload missing action_type")

    translated = _translate_action_params(action_type, params)
    result = execute_all(
        [{"type": action_type, "params": translated, "reason": "command"}]
    )
    return {
        "executed": result,
        "action_type": action_type,
        "params": translated,
    }


def _dispatch_goal(payload: dict) -> dict:
    """Add a new task or waiting-for item to goals.md."""
    from deja.goals import apply_tasks_update

    section = (payload.get("section") or "tasks").strip()
    text = (payload.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "Goal payload missing text")

    key = "add_tasks" if section == "tasks" else "add_waiting"
    changes = apply_tasks_update({key: [text]})
    return {"section": section, "text": text, "applied": changes}


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
        from deja.activity_log import append_log_entry

        append_log_entry("context", text[:120])
    except Exception:
        log.debug("activity_log append failed for context", exc_info=True)

    return {"observation_id": entry["id_key"], "priority": priority}


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.post("/api/command")
async def handle_command(body: CommandRequest) -> CommandResponse:
    """Classify and dispatch a user command. Single turn, no history."""
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
    else:
        raise HTTPException(400, f"Unknown command type: {cmd_type!r}")

    # Log every dispatched command in the wiki's activity log so the
    # Activity feed can render a row.
    try:
        from deja.activity_log import append_log_entry

        append_log_entry(
            "command", f"{cmd_type}: {confirmation or body.input[:120]}"
        )
    except Exception:
        log.debug("activity_log append failed for command", exc_info=True)

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
