"""Deja API server — proxies Gemini LLM calls for the Deja macOS app."""

import time
import logging

from fastapi import FastAPI, Request, Header, File, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from auth import validate_token
from proxy import generate, transcribe
from telemetry import log_event

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="Deja API", version="0.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# -- Models ------------------------------------------------------------------

class GenerateRequest(BaseModel):
    model: str
    contents: str | list
    config: dict = {}
    system_instruction: str | None = None
    tools: list | None = None


class TelemetryRequest(BaseModel):
    event: str
    properties: dict = {}
    client_version: str = "unknown"


# -- Routes ------------------------------------------------------------------

@app.get("/v1/health")
async def health():
    return {"status": "ok", "version": "0.2.0"}


@app.get("/v1/config")
async def config():
    return {
        "integrate_model": "gemini-2.5-flash-lite",
        "vision_model": "gemini-2.5-flash",
        "reflect_model": "gemini-2.5-pro",
    }


@app.post("/v1/generate")
async def generate_endpoint(
    body: GenerateRequest,
    authorization: str = Header(...),
):
    token = authorization.removeprefix("Bearer ").strip()
    user = await validate_token(token)

    start = time.time()
    result = await generate(
        body.model, body.contents, body.config,
        system_instruction=body.system_instruction,
        tools=body.tools,
    )
    latency_ms = round((time.time() - start) * 1000)

    input_tokens = result.get("usage_metadata", {}).get("prompt_token_count", 0)
    output_tokens = result.get("usage_metadata", {}).get("candidates_token_count", 0)

    logger.info(
        "generate user=%s model=%s in_tokens=%d out_tokens=%d latency_ms=%d",
        user["email"], body.model, input_tokens, output_tokens, latency_ms,
    )

    return result


@app.post("/v1/transcribe")
async def transcribe_endpoint(
    file: UploadFile = File(...),
    authorization: str = Header(...),
):
    token = authorization.removeprefix("Bearer ").strip()
    user = await validate_token(token)

    audio_bytes = await file.read()
    start = time.time()
    text = await transcribe(audio_bytes, file.filename or "audio.wav")
    latency_ms = round((time.time() - start) * 1000)

    logger.info(
        "transcribe user=%s size=%d latency_ms=%d text=%r",
        user["email"], len(audio_bytes), latency_ms, text[:100],
    )

    return {"text": text}


@app.post("/v1/telemetry")
async def telemetry_endpoint(
    body: TelemetryRequest,
    authorization: str | None = Header(None),
):
    user_email = None
    if authorization:
        try:
            token = authorization.removeprefix("Bearer ").strip()
            user = await validate_token(token)
            user_email = user["email"]
        except Exception:
            pass  # auth is optional for telemetry

    log_event(body.event, body.properties, user_email, body.client_version)
    return {"status": "ok"}
