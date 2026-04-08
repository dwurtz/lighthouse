"""GET/POST /api/chat — chat history and streaming conversation."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter

from deja.config import REFLECT_MODEL
from deja.prompts import load as load_prompt
from deja.web.helpers import (
    OBSERVATIONS_LOG,
    load_conversation,
    read_jsonl,
    save_conversation,
)

log = logging.getLogger("deja.chat")

router = APIRouter()


@router.get("/api/chat")
def get_chat(limit: int = 50) -> list[dict]:
    return load_conversation()[-limit:]


@router.post("/api/chat")
async def post_chat(body: dict):
    """Chat with Pro, with free-reign wiki tool access.

    The model can call ``list_pages`` / ``read_page`` / ``write_page`` /
    ``delete_page`` / ``rename_page`` directly. The agentic loop runs
    until Pro stops emitting function calls, then its final text reply
    is streamed to the client.
    """
    from starlette.responses import StreamingResponse

    user_message = body.get("message", "")
    exchange_start_ts = datetime.now(timezone.utc).isoformat()

    messages = load_conversation()
    messages.append(
        {
            "role": "user",
            "content": user_message,
            "timestamp": exchange_start_ts,
        }
    )
    save_conversation(messages)

    from deja.llm_client import GeminiClient, _USE_DIRECT

    if not _USE_DIRECT:
        # Chat with tool calling requires the full genai SDK.
        # TODO: Support tool calling through the proxy server.
        from starlette.responses import JSONResponse
        return JSONResponse(
            {"error": "Chat requires GEMINI_API_KEY (tool calling not yet supported through proxy)"},
            status_code=501,
        )

    gemini = GeminiClient()

    # Relevant wiki pages via QMD
    from deja.llm.search import search as qmd_search

    wiki_text = qmd_search(user_message, limit=5, collection="wiki")

    # Recent observations as peripheral context.
    observations = read_jsonl(OBSERVATIONS_LOG)
    recent_observations = ""
    for s in observations[-30:]:
        recent_observations += (
            f"[{s.get('timestamp', '')[:19]}] [{s.get('source', '')}] "
            f"{s.get('sender', '')}: {s.get('text', '')[:150]}\n"
        )

    from deja.identity import load_user

    user = load_user()
    user_fields = user.as_prompt_fields()

    history_lines = []
    for msg in messages[-20:]:
        role = user.first_name if msg["role"] == "user" else "Assistant"
        history_lines.append(f"{role}: {msg['content']}")

    from deja.wiki_schema import load_schema

    schema = load_schema()

    system_instruction = load_prompt("chat").format(
        schema=schema,
        wiki=wiki_text or "(no relevant pages)",
        recent_observations=recent_observations[-3000:] or "(none)",
        history=chr(10).join(history_lines[-10:]),
        **user_fields,
    )

    from deja.chat_tools import build_tool_declarations, execute_tool_call
    from google.genai import types as genai_types

    tools = build_tool_declarations()

    contents: list = [
        genai_types.Content(
            role="user",
            parts=[genai_types.Part.from_text(text=user_message)],
        ),
    ]

    MAX_TOOL_ROUNDS = 8

    async def stream_response():
        full_text_response = ""
        tool_events: list[dict] = []

        for round_idx in range(MAX_TOOL_ROUNDS):
            try:
                resp = await gemini.client.aio.models.generate_content(
                    model=REFLECT_MODEL,
                    contents=contents,
                    config=genai_types.GenerateContentConfig(
                        system_instruction=system_instruction,
                        tools=tools,
                        max_output_tokens=4096,
                        temperature=0.4,
                    ),
                )
            except Exception as e:
                err = f"chat LLM call failed: {e}"
                log.exception(err)
                yield f"data: {json.dumps({'chunk': f'[error] {err}'})}\n\n"
                break

            function_calls = []
            round_text = ""
            candidate = resp.candidates[0] if resp.candidates else None
            parts = (
                (candidate.content.parts if candidate and candidate.content else [])
                or []
            )
            for part in parts:
                if getattr(part, "function_call", None):
                    fc = part.function_call
                    function_calls.append(fc)
                elif getattr(part, "text", None):
                    round_text += part.text

            if parts:
                contents.append(genai_types.Content(role="model", parts=parts))

            if round_text:
                full_text_response += round_text
                yield f"data: {json.dumps({'chunk': round_text})}\n\n"

            if not function_calls:
                break

            response_parts = []
            for fc in function_calls:
                name = fc.name
                args = dict(fc.args) if fc.args else {}
                result = execute_tool_call(name, args)
                tool_events.append(
                    {
                        "tool": name,
                        "args": {
                            k: (
                                v
                                if not isinstance(v, str) or len(v) < 200
                                else v[:200] + "\u2026"
                            )
                            for k, v in args.items()
                        },
                        "ok": result.ok,
                        "message": result.message,
                    }
                )
                yield f"data: {json.dumps({'tool_call': tool_events[-1]})}\n\n"

                response_parts.append(
                    genai_types.Part.from_function_response(
                        name=name,
                        response=result.as_response_dict(),
                    )
                )

            contents.append(
                genai_types.Content(role="user", parts=response_parts)
            )
        else:
            warning = f"[chat tool loop hit {MAX_TOOL_ROUNDS}-round ceiling]"
            log.warning(warning)
            yield f"data: {json.dumps({'chunk': warning})}\n\n"

        # Commit wiki mutations from this chat turn.
        mutating = [
            e
            for e in tool_events
            if e["ok"] and e["tool"] in ("write_page", "delete_page", "rename_page")
        ]
        if mutating:
            try:
                from deja.wiki_git import commit_changes
                from deja.wiki_catalog import rebuild_index

                rebuild_index()
                summary = "; ".join(
                    f"{e['tool']} {e['args'].get('slug') or e['args'].get('old_slug', '?')}"
                    for e in mutating[:5]
                )
                if len(mutating) > 5:
                    summary += f" (+{len(mutating) - 5} more)"
                commit_changes(f"chat: {summary}")
            except Exception:
                log.exception("chat post-commit failed")

        # Save the full assistant turn to conversation.json.
        messages.append(
            {
                "role": "agent",
                "content": full_text_response or "(no narration)",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "tool_calls": tool_events or None,
            }
        )
        save_conversation(messages)

        # Write a single chat observation so reflect can see the exchange.
        now_ts = datetime.now(timezone.utc).isoformat()
        tool_summary = ""
        if tool_events:
            tool_summary = "\n  Tools: " + "; ".join(
                f"{e['tool']}({'ok' if e['ok'] else 'fail'})" for e in tool_events
            )
        exchange_text = (
            f"CONVERSATION with Agent (1 exchange, {exchange_start_ts[11:16]}-{now_ts[11:16]}):\n"
            f"  {user.first_name}: {user_message}\n"
            f"  Agent: {full_text_response}{tool_summary}"
        )
        id_key = "chat-" + hashlib.md5(
            f"{exchange_start_ts}-{user_message[:100]}-{full_text_response[:100]}".encode()
        ).hexdigest()[:16]
        try:
            with open(OBSERVATIONS_LOG, "a") as f:
                f.write(
                    json.dumps(
                        {
                            "source": "chat",
                            "sender": f"Agent \u2194 {user.first_name}",
                            "text": exchange_text[:2000],
                            "timestamp": exchange_start_ts,
                            "id_key": id_key,
                        }
                    )
                    + "\n"
                )
        except Exception:
            log.exception("failed to persist chat observation")

        # Human-readable entry in log.md.
        from deja.activity_log import append_log_entry

        one_line_user = " ".join((user_message or "").split())[:120]
        suffix = f" [{len(mutating)} wiki edit(s)]" if mutating else ""
        append_log_entry("chat", f"{user.first_name}: {one_line_user}{suffix}")

        yield f"data: {json.dumps({'done': True})}\n\n"

    return StreamingResponse(stream_response(), media_type="text/event-stream")
