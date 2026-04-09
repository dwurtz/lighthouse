"""Deja API proxy — Gemini LLM (with tool calling) + Groq Whisper transcription."""

import base64
import os

import httpx
from google import genai
from google.genai import types
from fastapi import HTTPException


def _get_client() -> genai.Client:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="GEMINI_API_KEY not configured")
    return genai.Client(api_key=api_key)


def _deserialize_contents(contents):
    """Convert JSON-serialized contents back to SDK types.

    Handles:
    - Plain strings
    - base64-encoded binary parts: {"type": "bytes", "data": "<b64>", "mime_type": "..."}
    - Role-based content: {"role": "user"|"model", "parts": [...]}
    - Function call parts: {"function_call": {"name": "...", "args": {...}}}
    - Function response parts: {"function_response": {"name": "...", "response": {...}}}
    """
    if isinstance(contents, str):
        return contents
    if not isinstance(contents, list):
        return contents

    result = []
    for item in contents:
        if isinstance(item, str):
            result.append(item)
        elif isinstance(item, dict) and item.get("type") == "bytes":
            data = base64.b64decode(item["data"])
            result.append(types.Part.from_bytes(data=data, mime_type=item["mime_type"]))
        elif isinstance(item, dict) and "role" in item and "parts" in item:
            # Content with role + parts
            parts = []
            for p in (item.get("parts") or []):
                parts.append(_deserialize_part(p))
            result.append(types.Content(role=item["role"], parts=parts))
        elif isinstance(item, dict):
            result.append(item)
        else:
            result.append(item)
    return result


def _deserialize_part(p):
    """Deserialize a single part dict into an SDK Part."""
    if isinstance(p, str):
        return types.Part.from_text(text=p)
    if isinstance(p, dict):
        if "text" in p:
            return types.Part.from_text(text=p["text"])
        if "function_call" in p:
            fc = p["function_call"]
            # Build FunctionCall with thought_signature if present.
            # Must use the types constructor directly — from_function_call()
            # doesn't support thought_signature.
            fc_kwargs = {
                "name": fc["name"],
                "args": fc.get("args", {}),
            }
            if "thought_signature" in fc and fc["thought_signature"]:
                fc_kwargs["thought_signature"] = fc["thought_signature"]
            return types.Part(function_call=types.FunctionCall(**fc_kwargs))
        if p.get("thought"):
            # Thought parts must be round-tripped exactly — Gemini
            # requires them in the contents for multi-turn tool calls.
            return types.Part(thought=True, text=p.get("text", ""))
        if "function_response" in p:
            fr = p["function_response"]
            return types.Part.from_function_response(
                name=fr["name"],
                response=fr.get("response", {}),
            )
        if p.get("type") == "bytes":
            data = base64.b64decode(p["data"])
            return types.Part.from_bytes(data=data, mime_type=p["mime_type"])
    return p


def _deserialize_tools(tools_data):
    """Convert JSON tool declarations to SDK Tool objects."""
    if not tools_data:
        return None

    all_decls = []
    for tool_group in tools_data:
        decls = tool_group.get("function_declarations", [])
        for d in decls:
            all_decls.append(types.FunctionDeclaration(
                name=d["name"],
                description=d.get("description", ""),
                parameters=d.get("parameters"),
            ))

    if all_decls:
        return [types.Tool(function_declarations=all_decls)]
    return None


def _serialize_response(response) -> dict:
    """Serialize a Gemini response to JSON, preserving all parts including thought_signature."""
    candidate = response.candidates[0] if response.candidates else None
    parts_data = []
    text = ""

    if candidate and candidate.content and candidate.content.parts:
        for part in candidate.content.parts:
            if getattr(part, "function_call", None):
                fc = part.function_call
                fc_data = {
                    "name": fc.name,
                    "args": dict(fc.args) if fc.args else {},
                }
                # Preserve thought_signature — required by Gemini 3.1 Pro
                if hasattr(fc, "thought_signature") and fc.thought_signature:
                    fc_data["thought_signature"] = fc.thought_signature
                parts_data.append({"function_call": fc_data})
            elif getattr(part, "thought", None) is not None:
                # Preserve thought parts for multi-round
                parts_data.append({"thought": True, "text": part.text or ""})
            elif getattr(part, "text", None):
                text += part.text
                parts_data.append({"text": part.text})

    usage = {}
    if hasattr(response, "usage_metadata") and response.usage_metadata:
        try:
            usage = response.usage_metadata.model_dump()
        except Exception:
            usage = {}

    return {
        "text": text,
        "parts": parts_data,
        "usage_metadata": usage,
    }


async def generate(
    model: str,
    contents,
    config: dict,
    system_instruction: str | None = None,
    tools: list | None = None,
) -> dict:
    """Proxy a generateContent call to Gemini. Returns serialized response."""
    import logging
    logger = logging.getLogger("proxy")

    client = _get_client()

    try:
        contents = _deserialize_contents(contents)
        sdk_tools = _deserialize_tools(tools)
    except Exception as exc:
        logger.exception("Failed to deserialize request")
        raise HTTPException(status_code=400, detail="Invalid request format")

    try:
        gen_config = types.GenerateContentConfig(**(config or {}))
        if system_instruction:
            gen_config.system_instruction = system_instruction
        if sdk_tools:
            gen_config.tools = sdk_tools
    except Exception as exc:
        logger.exception("Failed to build GenerateContentConfig")
        raise HTTPException(status_code=400, detail="Invalid configuration")

    # Log the contents structure for debugging tool-call round-trips
    import json as _json
    try:
        def _debug_contents(c):
            if isinstance(c, list):
                return [_debug_contents(x) for x in c]
            if hasattr(c, 'role'):
                parts_debug = []
                for p in (c.parts or []):
                    if hasattr(p, 'function_call') and p.function_call:
                        fc = p.function_call
                        parts_debug.append(f"function_call:{fc.name} has_thought_sig={hasattr(fc, 'thought_signature') and bool(fc.thought_signature)}")
                    elif hasattr(p, 'function_response') and p.function_response:
                        parts_debug.append(f"function_response:{p.function_response.name}")
                    elif hasattr(p, 'thought') and p.thought:
                        parts_debug.append("thought")
                    elif hasattr(p, 'text') and p.text:
                        parts_debug.append(f"text:{len(p.text)}chars")
                return f"{c.role}:[{', '.join(parts_debug)}]"
            return str(type(c).__name__)
        logger.info("generate contents: %s", _debug_contents(contents))
    except Exception:
        pass

    try:
        response = client.models.generate_content(
            model=model,
            contents=contents,
            config=gen_config,
        )
    except genai.errors.ClientError as exc:
        logger.error("Gemini ClientError: %s", exc)
        raise HTTPException(status_code=400, detail="LLM request failed — check model and parameters")
    except Exception as exc:
        logger.exception("Gemini generate failed")
        raise HTTPException(status_code=502, detail="LLM service temporarily unavailable")

    return _serialize_response(response)


async def transcribe(audio_bytes: bytes, filename: str = "audio.wav") -> str:
    """Transcribe audio via Groq Whisper API. Returns transcript text."""
    groq_key = os.environ.get("GROQ_API_KEY")
    if not groq_key:
        raise HTTPException(status_code=500, detail="GROQ_API_KEY not configured")

    mime = "audio/wav" if filename.endswith(".wav") else "audio/mpeg"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {groq_key}"},
            files={"file": (filename, audio_bytes, mime)},
            data={"model": "whisper-large-v3"},
        )
        resp.raise_for_status()
        return (resp.json().get("text") or "").strip()
