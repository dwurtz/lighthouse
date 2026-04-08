"""Gemini proxy — forwards generate_content calls using the server's API key."""

import os

from google import genai
from fastapi import HTTPException


def _get_client() -> genai.Client:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="GEMINI_API_KEY not configured")
    return genai.Client(api_key=api_key)


async def generate(model: str, contents, config: dict) -> dict:
    """Proxy a generateContent call to Gemini. Returns the raw response dict."""
    client = _get_client()

    try:
        response = client.models.generate_content(
            model=model,
            contents=contents,
            config=config or None,
        )
    except genai.errors.ClientError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Gemini error: {exc}")

    # Serialize the response to a dict the client can consume.
    # The genai SDK response has a model_dump or to_dict depending on version.
    if hasattr(response, "model_dump"):
        return response.model_dump()
    elif hasattr(response, "to_json_dict"):
        return response.to_json_dict()
    else:
        # Fallback: pull the essential fields
        return {
            "text": response.text if hasattr(response, "text") else str(response),
            "usage_metadata": (
                response.usage_metadata.model_dump()
                if hasattr(response, "usage_metadata") and response.usage_metadata
                else {}
            ),
        }
