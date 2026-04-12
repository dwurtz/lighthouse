"""Deja API server — proxies Gemini LLM calls for the Deja macOS app."""

import time
import logging

from fastapi import FastAPI, Request, Header, File, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from auth import validate_token
from proxy import generate, transcribe, groq_chat
from telemetry import log_event

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="Deja API", version="0.2.0")

# Rate limiting
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# CORS restricted — native macOS client doesn't need CORS, but keep
# trydeja.com for the admin dashboard and potential web client.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://trydeja.com", "http://localhost:5055"],
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
)


# -- Models ------------------------------------------------------------------

class GenerateRequest(BaseModel):
    model: str
    contents: str | list
    config: dict = {}
    system_instruction: str | None = None
    tools: list | None = None


class ChatRequest(BaseModel):
    """OpenAI-compatible chat completion request, routed to Groq."""
    model: str
    messages: list[dict]
    temperature: float = 0.1
    max_tokens: int = 2048
    response_format: dict | None = None


class TelemetryRequest(BaseModel):
    event: str
    properties: dict = {}
    client_version: str = "unknown"


# -- Routes ------------------------------------------------------------------

@app.get("/v1/health")
async def health():
    """Check proxy reachability + every dependency that could break LLM calls.

    Returns ``{"status": "ok"|"degraded"|"broken", "checks": {...}, "version": ...}``.
    Individual checks are fast (<500ms total typical) and never raise —
    the point is to surface dependency failure as a structured response
    the client can act on, not to fail the health probe itself.

    Status aggregation:
      - ``broken``   — any check failed in a way that blocks LLM calls
                       (no Gemini key, no Groq key, DB unreachable).
      - ``degraded`` — something slow or a non-critical check failed
                       (Gemini reachable but taking >2s).
      - ``ok``       — everything passes.
    """
    import os
    import time
    import httpx

    checks: dict = {}
    t0 = time.monotonic()

    # 1. Gemini key configured — without this every /v1/generate fails
    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    checks["gemini_key"] = {
        "ok": bool(gemini_key),
        "detail": "configured" if gemini_key else "missing GEMINI_API_KEY env var",
    }

    # 2. Groq key configured — used by voice polish + triage
    groq_key = os.environ.get("GROQ_API_KEY", "")
    checks["groq_key"] = {
        "ok": bool(groq_key),
        "detail": "configured" if groq_key else "missing GROQ_API_KEY env var",
    }

    # 3. Telemetry DB reachable + writable
    try:
        from db import _get_db
        db = _get_db()
        db.execute("SELECT 1").fetchone()
        db.close()
        checks["telemetry_db"] = {"ok": True, "detail": "reachable"}
    except Exception as e:
        checks["telemetry_db"] = {"ok": False, "detail": f"{type(e).__name__}: {e}"[:200]}

    # 4. Gemini endpoint reachability (HEAD request, 3s timeout)
    #    Fast probe — if Gemini's HTTP endpoint is down or slow, every
    #    subsequent /v1/generate call will fail or stall. Doing this
    #    check in the health path means the client can see the problem
    #    immediately and surface it via the error toast.
    gemini_t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get("https://generativelanguage.googleapis.com/")
        gemini_ms = int((time.monotonic() - gemini_t0) * 1000)
        reachable = 200 <= r.status_code < 500  # 404 is fine, it means we hit the server
        checks["gemini_endpoint"] = {
            "ok": reachable,
            "detail": f"{r.status_code} in {gemini_ms}ms",
            "latency_ms": gemini_ms,
        }
    except Exception as e:
        checks["gemini_endpoint"] = {
            "ok": False,
            "detail": f"{type(e).__name__}: {e}"[:200],
        }

    # Aggregate status
    critical = {"gemini_key", "telemetry_db", "gemini_endpoint"}
    broken = any(not checks[k]["ok"] for k in critical)
    degraded = (
        not broken
        and checks["gemini_endpoint"].get("latency_ms", 0) > 2000
    ) or not checks["groq_key"]["ok"]
    status = "broken" if broken else ("degraded" if degraded else "ok")

    return {
        "status": status,
        "version": "0.2.0",
        "checks": checks,
        "total_ms": int((time.monotonic() - t0) * 1000),
    }


def _user_feature_flags(email: str | None) -> dict:
    """Return per-user feature flags driven by env vars.

    Each flag env var is a comma-separated list of email addresses.
    Empty/unset = flag is off for everyone.
    """
    if not email:
        return {}

    def _enabled_for(env_var: str) -> bool:
        allowed = os.environ.get(env_var, "")
        if not allowed:
            return False
        emails = [e.strip().lower() for e in allowed.split(",") if e.strip()]
        return email.lower() in emails

    return {
        "vision_shadow_eval": _enabled_for("VISION_SHADOW_EVAL_USERS"),
    }


@app.get("/v1/config")
async def config(authorization: str | None = Header(None)):
    flags: dict = {}
    if authorization:
        try:
            token = authorization.removeprefix("Bearer ").strip()
            user = await validate_token(token)
            flags = _user_feature_flags(user.get("email"))
        except Exception:
            pass  # auth optional for /v1/config — anonymous users get defaults

    return {
        "integrate_model": "gemini-2.5-flash-lite",
        "vision_model": "gemini-2.5-flash",
        "reflect_model": "gemini-2.5-pro",
        "oauth_client_id": os.environ.get("GOOGLE_CLIENT_ID", ""),
        "oauth_client_secret": os.environ.get("GOOGLE_CLIENT_SECRET", ""),
        "oauth_project_id": "deja-ai-app",
        "feature_flags": flags,
    }


ALLOWED_MODELS = {
    "gemini-2.5-flash-lite", "gemini-2.5-flash", "gemini-2.5-pro",
    "gemini-2.0-flash", "gemini-2.0-flash-lite",
    "gemini-3.1-pro-preview", "gemini-3.1-flash-lite-preview",
    "gemini-3-flash-preview",
}

# Groq chat models we allow through /v1/chat. Start narrow — add as needed.
# llama-3.1-8b-instant is the Flash-Lite equivalent: ~800 tok/s, $0.05/$0.08
# per 1M input/output, used for the voice-pill polish pass.
ALLOWED_GROQ_MODELS = {
    "llama-3.1-8b-instant",
}

MAX_AUDIO_SIZE = 25 * 1024 * 1024  # 25MB


@app.post("/v1/generate")
@limiter.limit("60/minute")
async def generate_endpoint(
    request: Request,
    body: GenerateRequest,
    authorization: str = Header(...),
    x_request_id: str | None = Header(None),
):
    request_id = x_request_id or "no-id"
    token = authorization.removeprefix("Bearer ").strip()
    user = await validate_token(token)

    if body.model not in ALLOWED_MODELS:
        return JSONResponse(
            status_code=400,
            content={"error": f"Model not allowed: {body.model}"},
        )

    start = time.time()
    result = await generate(
        body.model, body.contents, body.config,
        system_instruction=body.system_instruction,
        tools=body.tools,
    )
    latency_ms = round((time.time() - start) * 1000)

    input_tokens = result.get("usage_metadata", {}).get("prompt_token_count", 0)
    output_tokens = result.get("usage_metadata", {}).get("candidates_token_count", 0)
    # Implicit context cache hit count. Gemini returns this on any call
    # where part of the input prefix was served from a cached entry.
    # We log it as an additional column so we can compute cache hit rate
    # (cached / input) and prompt-restructure savings retroactively.
    # Expected ~0 until prompts are explicitly restructured with static
    # content at the top; grep `cached=` in Render logs to measure.
    cached_tokens = result.get("usage_metadata", {}).get("cached_content_token_count", 0)

    logger.info(
        "generate rid=%s user=%s model=%s in=%d cached=%d out=%d ms=%d",
        request_id, user["email"], body.model,
        input_tokens, cached_tokens, output_tokens, latency_ms,
    )

    return result


@app.post("/v1/chat")
@limiter.limit("120/minute")
async def chat_endpoint(
    request: Request,
    body: ChatRequest,
    authorization: str = Header(...),
    x_request_id: str | None = Header(None),
):
    """OpenAI-compatible chat completion, routed to Groq.

    Used by low-latency LLM paths (voice-pill polish, etc.) where
    Groq's LPU throughput matters more than Gemini's structured-output
    quality. Whitelisted to the models in ALLOWED_GROQ_MODELS.
    """
    request_id = x_request_id or "no-id"
    token = authorization.removeprefix("Bearer ").strip()
    user = await validate_token(token)

    if body.model not in ALLOWED_GROQ_MODELS:
        return JSONResponse(
            status_code=400,
            content={"error": f"Model not allowed: {body.model}"},
        )

    start = time.time()
    text = await groq_chat(
        model=body.model,
        messages=body.messages,
        temperature=body.temperature,
        max_tokens=body.max_tokens,
        response_format=body.response_format,
    )
    latency_ms = round((time.time() - start) * 1000)

    logger.info(
        "chat rid=%s user=%s model=%s chars=%d ms=%d",
        request_id, user["email"], body.model, len(text), latency_ms,
    )

    return {"text": text}


@app.post("/v1/transcribe")
@limiter.limit("10/minute")
async def transcribe_endpoint(
    request: Request,
    file: UploadFile = File(...),
    authorization: str = Header(...),
):
    token = authorization.removeprefix("Bearer ").strip()
    user = await validate_token(token)

    audio_bytes = await file.read()
    if len(audio_bytes) > MAX_AUDIO_SIZE:
        return JSONResponse(status_code=413, content={"error": "File too large (25MB max)"})

    start = time.time()
    text = await transcribe(audio_bytes, file.filename or "audio.wav")
    latency_ms = round((time.time() - start) * 1000)

    logger.info(
        "transcribe user=%s size=%d ms=%d",
        user["email"], len(audio_bytes), latency_ms,
    )

    return {"text": text}


@app.post("/v1/telemetry")
@limiter.limit("120/minute")
async def telemetry_endpoint(
    request: Request,
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
import html as _html
from fastapi.responses import HTMLResponse
from db import search_events, get_stats, get_user_detail

def _esc(s) -> str:
    """HTML-escape user-controlled strings for safe rendering."""
    return _html.escape(str(s)) if s else ""

ADMIN_KEY = os.environ.get("DEJA_ADMIN_KEY")
if not ADMIN_KEY:
    logger.warning("DEJA_ADMIN_KEY not set — admin dashboard disabled")


@app.get("/admin")
async def admin_dashboard(key: str = ""):
    if not ADMIN_KEY or key != ADMIN_KEY:
        return HTMLResponse("<h1>Unauthorized</h1>", status_code=401)

    stats = get_stats()
    errors_html = ""
    for e in stats["recent_errors"]:
        props = _esc(e.get("properties", "{}"))
        errors_html += f"<tr><td>{_esc(e['timestamp'][:19])}</td><td>{_esc(e['user_email']) or '—'}</td><td>{props}</td></tr>"

    users_html = ""
    for u in stats["active_users"]:
        email = u["user_email"]
        users_html += (
            f"<tr><td><a href=\"/admin/user?key={_esc(key)}&email={_esc(email)}\">{_esc(email)}</a></td>"
            f"<td>{u['event_count']}</td>"
            f"<td>{_esc(u['last_seen'][:19])}</td></tr>"
        )

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
            f"<tr><td>{_esc(r['timestamp'][:19])}</td><td>{_esc(r['event'])}</td>"
            f"<td>{_esc(r['user_email']) or '—'}</td><td>{_esc(r['request_id']) or '—'}</td>"
            f"<td><small>{_esc(r['properties'][:200])}</small></td></tr>"
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
<h1><a href="/admin?key={_esc(key)}">← Déjà Admin</a> — Search: "{_esc(q or event)}"</h1>

<form action="/admin/search" method="get">
  <input type="hidden" name="key" value="{_esc(key)}">
  <input type="text" name="q" placeholder="Request ID, email, or keyword..." value="{_esc(q)}">
  <button type="submit">Search</button>
</form>

<p>{len(results)} result(s)</p>
<table><tr><th>Time</th><th>Event</th><th>User</th><th>Request ID</th><th>Details</th></tr>{rows_html}</table>

</body></html>""")


@app.get("/admin/user")
async def admin_user_detail(key: str = "", email: str = ""):
    """Per-user drill-down — aggregate counts, event breakdown, timeline.

    Accessed by clicking an email on the main dashboard. Shows everything
    the proxy knows about one user without requiring a free-text search.
    """
    if not ADMIN_KEY or key != ADMIN_KEY:
        return HTMLResponse("<h1>Unauthorized</h1>", status_code=401)
    if not email:
        return HTMLResponse("<h1>Missing email</h1>", status_code=400)

    detail = get_user_detail(email, limit=100)
    profile = detail["profile"]

    if not profile or not profile.get("total_events"):
        return HTMLResponse(f"<h1>No events for {_esc(email)}</h1>", status_code=404)

    first_seen = profile.get("first_seen", "")[:19]
    last_seen = profile.get("last_seen", "")[:19]

    breakdown_html = ""
    for row in detail["event_breakdown"]:
        breakdown_html += f"<tr><td>{_esc(row['event'])}</td><td>{row['count']}</td></tr>"

    timeline_html = ""
    for e in detail["recent_events"]:
        timeline_html += (
            f"<tr><td>{_esc(e['timestamp'][:19])}</td>"
            f"<td>{_esc(e['event'])}</td>"
            f"<td>{_esc(e['request_id']) or '—'}</td>"
            f"<td><small>{_esc((e['properties'] or '')[:200])}</small></td></tr>"
        )

    return HTMLResponse(f"""<!DOCTYPE html>
<html><head><title>Déjà Admin — {_esc(email)}</title>
<style>
  body {{ font-family: -apple-system, sans-serif; max-width: 1100px; margin: 40px auto; background: #111; color: #eee; padding: 0 20px; }}
  h1 {{ color: #fff; }} h2 {{ color: #aaa; margin-top: 32px; }}
  a {{ color: #8cf; }}
  .stats {{ display: flex; gap: 16px; margin: 20px 0; flex-wrap: wrap; }}
  .stat {{ background: #222; padding: 14px 20px; border-radius: 8px; min-width: 110px; }}
  .stat-num {{ font-size: 22px; font-weight: bold; color: #fff; }}
  .stat-label {{ font-size: 11px; color: #888; margin-top: 4px; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 8px; }}
  th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid #333; font-size: 12px; vertical-align: top; }}
  th {{ color: #888; font-weight: 600; }}
  .meta {{ color: #888; font-size: 13px; margin-top: 8px; }}
  .error {{ color: #f66; }}
</style></head><body>
<h1><a href="/admin?key={_esc(key)}">← Déjà Admin</a> — {_esc(email)}</h1>
<div class="meta">
  Client: <strong>{_esc(detail['current_version'])}</strong> ·
  First seen: {_esc(first_seen)} ·
  Last seen: {_esc(last_seen)}
</div>

<div class="stats">
  <div class="stat"><div class="stat-num">{profile.get('total_events', 0)}</div><div class="stat-label">Total events</div></div>
  <div class="stat"><div class="stat-num">{profile.get('cycles', 0)}</div><div class="stat-label">Cycles run</div></div>
  <div class="stat"><div class="stat-num">{profile.get('commands', 0)}</div><div class="stat-label">Commands</div></div>
  <div class="stat"><div class="stat-num">{profile.get('llm_calls', 0)}</div><div class="stat-label">LLM calls</div></div>
  <div class="stat"><div class="stat-num error">{profile.get('errors', 0)}</div><div class="stat-label">Errors</div></div>
</div>

<h2>Event breakdown</h2>
<table><tr><th>Event</th><th>Count</th></tr>{breakdown_html}</table>

<h2>Recent events (last 100)</h2>
<table><tr><th>Time</th><th>Event</th><th>Request ID</th><th>Details</th></tr>{timeline_html}</table>

</body></html>""")
