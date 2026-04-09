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
    x_request_id: str | None = Header(None),
):
    request_id = x_request_id or "no-id"
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
        "generate rid=%s user=%s model=%s in_tokens=%d out_tokens=%d latency_ms=%d",
        request_id, user["email"], body.model, input_tokens, output_tokens, latency_ms,
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


# -- Admin Dashboard ----------------------------------------------------------

import os
from fastapi.responses import HTMLResponse
from db import search_events, get_stats

ADMIN_KEY = os.environ.get("DEJA_ADMIN_KEY", "admin")


@app.get("/admin")
async def admin_dashboard(key: str = ""):
    if key != ADMIN_KEY:
        return HTMLResponse("<h1>Unauthorized</h1><p>Add ?key=YOUR_ADMIN_KEY</p>", status_code=401)

    stats = get_stats()
    errors_html = ""
    for e in stats["recent_errors"]:
        props = e.get("properties", "{}")
        errors_html += f"<tr><td>{e['timestamp'][:19]}</td><td>{e['user_email'] or '—'}</td><td>{props}</td></tr>"

    users_html = ""
    for u in stats["active_users"]:
        users_html += f"<tr><td>{u['user_email']}</td><td>{u['event_count']}</td><td>{u['last_seen'][:19]}</td></tr>"

    return HTMLResponse(f"""<!DOCTYPE html>
<html><head><title>Déjà Admin</title>
<style>
  body {{ font-family: -apple-system, sans-serif; max-width: 900px; margin: 40px auto; background: #111; color: #eee; padding: 0 20px; }}
  h1 {{ color: #fff; }} h2 {{ color: #aaa; margin-top: 32px; }}
  .stats {{ display: flex; gap: 24px; margin: 20px 0; }}
  .stat {{ background: #222; padding: 16px 24px; border-radius: 8px; }}
  .stat-num {{ font-size: 28px; font-weight: bold; color: #fff; }}
  .stat-label {{ font-size: 12px; color: #888; margin-top: 4px; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 8px; }}
  th, td {{ text-align: left; padding: 8px 12px; border-bottom: 1px solid #333; font-size: 13px; }}
  th {{ color: #888; font-weight: 600; }}
  form {{ margin: 20px 0; display: flex; gap: 8px; }}
  input {{ background: #222; border: 1px solid #444; color: #fff; padding: 8px 12px; border-radius: 6px; font-size: 14px; width: 300px; }}
  button {{ background: #fff; color: #000; border: none; padding: 8px 16px; border-radius: 6px; cursor: pointer; font-size: 14px; }}
  .error {{ color: #f66; }}
</style></head><body>
<h1>Déjà Admin</h1>

<div class="stats">
  <div class="stat"><div class="stat-num">{stats['unique_users']}</div><div class="stat-label">Users</div></div>
  <div class="stat"><div class="stat-num">{stats['total_events']}</div><div class="stat-label">Events</div></div>
  <div class="stat"><div class="stat-num error">{stats['total_errors']}</div><div class="stat-label">Errors</div></div>
</div>

<h2>Search</h2>
<form action="/admin/search" method="get">
  <input type="hidden" name="key" value="{key}">
  <input type="text" name="q" placeholder="Request ID, email, or keyword...">
  <button type="submit">Search</button>
</form>

<h2>Active Users</h2>
<table><tr><th>Email</th><th>Events</th><th>Last Seen</th></tr>{users_html}</table>

<h2>Recent Errors</h2>
<table><tr><th>Time</th><th>User</th><th>Details</th></tr>{errors_html}</table>

</body></html>""")


@app.get("/admin/search")
async def admin_search(key: str = "", q: str = "", event: str = ""):
    if key != ADMIN_KEY:
        return HTMLResponse("<h1>Unauthorized</h1>", status_code=401)

    results = search_events(query=q, event_type=event, limit=200)

    rows_html = ""
    for r in results:
        rows_html += (
            f"<tr><td>{r['timestamp'][:19]}</td><td>{r['event']}</td>"
            f"<td>{r['user_email'] or '—'}</td><td>{r['request_id'] or '—'}</td>"
            f"<td><small>{r['properties'][:200]}</small></td></tr>"
        )

    return HTMLResponse(f"""<!DOCTYPE html>
<html><head><title>Déjà Admin — Search</title>
<style>
  body {{ font-family: -apple-system, sans-serif; max-width: 1100px; margin: 40px auto; background: #111; color: #eee; padding: 0 20px; }}
  h1 {{ color: #fff; }} a {{ color: #8cf; }}
  form {{ margin: 20px 0; display: flex; gap: 8px; }}
  input {{ background: #222; border: 1px solid #444; color: #fff; padding: 8px 12px; border-radius: 6px; font-size: 14px; width: 300px; }}
  button {{ background: #fff; color: #000; border: none; padding: 8px 16px; border-radius: 6px; cursor: pointer; font-size: 14px; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 8px; }}
  th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid #333; font-size: 12px; }}
  th {{ color: #888; font-weight: 600; }}
  .error {{ color: #f66; }}
</style></head><body>
<h1><a href="/admin?key={key}">← Déjà Admin</a> — Search: "{q or event}"</h1>

<form action="/admin/search" method="get">
  <input type="hidden" name="key" value="{key}">
  <input type="text" name="q" placeholder="Request ID, email, or keyword..." value="{q}">
  <button type="submit">Search</button>
</form>

<p>{len(results)} result(s)</p>
<table><tr><th>Time</th><th>Event</th><th>User</th><th>Request ID</th><th>Details</th></tr>{rows_html}</table>

</body></html>""")
