"""Deja API server — proxies Gemini LLM calls for the Deja macOS app."""

import time
import logging

from fastapi import FastAPI, Request, Header, File, UploadFile, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from auth import validate_token
from proxy import generate, transcribe, groq_chat
from telemetry import log_event

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="Deja API", version="0.3.0")

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
    allow_headers=["Authorization", "Content-Type", "X-Request-ID", "X-Deja-Request-Id"],
    expose_headers=["X-Deja-Request-Id"],
)


# Request-ID correlation middleware. Swift menubar generates
# ``swift_<uuid>`` and forwards it via the local Python backend on every
# /v1/* call; we echo it back and prefix every log line with
# ``[rid=...]`` so a single user action can be traced across all three
# layers (Swift → local Python → server). Missing header = server-side
# fallback so older clients keep working.
@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    import uuid as _uuid
    rid = request.headers.get("x-deja-request-id") or f"srv_{_uuid.uuid4().hex}"
    request.state.rid = rid
    path = request.url.path
    log_it = path.startswith("/v1/")
    if log_it:
        ua = request.headers.get("user-agent", "")
        logger.info(
            "[rid=%s] %s %s user_agent=%s",
            rid, request.method, path, ua,
        )
    start = time.time()
    try:
        response = await call_next(request)
    except Exception:
        duration_ms = round((time.time() - start) * 1000)
        logger.exception(
            "[rid=%s] %s %s -> 500 (%dms)",
            rid, request.method, path, duration_ms,
        )
        raise
    duration_ms = round((time.time() - start) * 1000)
    response.headers["X-Deja-Request-Id"] = rid
    if log_it:
        logger.info(
            "[rid=%s] %s %s -> %d (%dms)",
            rid, request.method, path, response.status_code, duration_ms,
        )
    return response


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


class DiagnosticRequest(BaseModel):
    """Log bundle uploaded by the desktop client for support/debugging.

    ``bundle`` is a plain-text concatenation of deja.log, errors.jsonl,
    audit.jsonl, Swift ``log show`` output, and system metadata. Capped
    at 2MB server-side; client trims before sending.
    """
    bundle: str
    note: str = ""
    client_version: str = "unknown"


# -- Routes ------------------------------------------------------------------

@app.get("/v1/me")
async def whoami(request: Request, authorization: str = Header("")):
    """Identity + admin check for the logged-in client.

    Called by the desktop app at launch so the tray menu can decide
    whether to show the "Open Admin Dashboard" option. Non-admin
    users get ``is_admin: false`` and the option stays hidden. The
    admin allowlist lives server-side (env var ``DEJA_ADMIN_EMAILS``)
    so revoking access is one Render config change, not a client
    update.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing token")
    token = authorization[7:]
    user = await validate_token(token)
    email = user.get("email", "")
    return {
        "email": email,
        "name": user.get("name", ""),
        "is_admin": email.lower() in _admin_emails(),
    }


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

    Currently empty — vision shadow eval was retired after the FastVLM
    path was replaced with Apple Vision OCR, and integrate shadow eval
    moved to a hardcoded-off client flag. Add new flags here when a
    real A/B starts again.
    """
    return {}


@app.get("/v1/config")
async def config(authorization: str | None = Header(None)):
    return {
        "integrate_model": "gemini-2.5-flash-lite",
        "vision_model": "gemini-2.5-flash",
        "reflect_model": "gemini-2.5-pro",
        "oauth_client_id": os.environ.get("GOOGLE_CLIENT_ID", ""),
        "oauth_client_secret": os.environ.get("GOOGLE_CLIENT_SECRET", ""),
        "oauth_project_id": "deja-ai-app",
        "feature_flags": _user_feature_flags(None),
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

    # Gemini returns usage_metadata keys as explicit null (not missing)
    # when there's no value — e.g. `cached_content_token_count: null` on
    # a cache miss. `dict.get("x", 0)` returns None (not 0) in that
    # case, and the %d in the log format string then crashes the entire
    # log line. Coerce with `or 0` to be safe across all three fields.
    usage = result.get("usage_metadata") or {}
    input_tokens = usage.get("prompt_token_count") or 0
    output_tokens = usage.get("candidates_token_count") or 0
    cached_tokens = usage.get("cached_content_token_count") or 0

    logger.info(
        "[rid=%s] generate legacy_rid=%s user=%s model=%s in=%d cached=%d out=%d ms=%d",
        request.state.rid, request_id, user["email"], body.model,
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
        "[rid=%s] chat legacy_rid=%s user=%s model=%s chars=%d ms=%d",
        request.state.rid, request_id, user["email"], body.model, len(text), latency_ms,
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
        "[rid=%s] transcribe user=%s size=%d ms=%d",
        request.state.rid, user["email"], len(audio_bytes), latency_ms,
    )

    return {"text": text}


@app.get("/mobile/setup")
async def mobile_setup_page(k: str = "", label: str = "mobile"):
    """Tiny landing page for iPhone Camera scans of the setup QR.

    The ``deja mobile create-key --qr`` command generates a QR code
    encoding ``https://deja-api.onrender.com/mobile/setup?k=<key>&label=<label>``.
    The user's iPhone Camera recognizes it as a URL, offers to open in
    Safari, and this page then shows the key + copy button + the
    instructions for setting up the Shortcut.

    No auth — the key in the URL is the capability. Whoever has the
    link has the key; this is the same trust boundary as the terminal
    printing it in plaintext. Don't share the URL.
    """
    from fastapi.responses import HTMLResponse
    safe_k = (k or "").replace("<", "").replace(">", "").replace('"', "")[:200]
    safe_label = (label or "mobile").replace("<", "").replace(">", "").replace('"', "")[:40]
    if not safe_k.startswith("deja_"):
        return HTMLResponse(
            "<h1>Invalid or missing key</h1>"
            "<p>Run <code>deja mobile create-key --qr</code> on your Mac.</p>",
            status_code=400,
        )
    html = f"""<!doctype html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Deja mobile key</title>
<style>
  :root {{ color-scheme: dark light; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, system-ui, sans-serif;
          margin: 0; padding: 24px; max-width: 560px; line-height: 1.45; }}
  h1 {{ font-size: 20px; margin-top: 0; }}
  .label {{ color: #888; font-size: 13px; text-transform: uppercase;
            letter-spacing: 0.5px; margin-bottom: 4px; }}
  .key {{ font-family: ui-monospace, Menlo, monospace; font-size: 13px;
          padding: 12px; background: rgba(127,127,127,0.12); border-radius: 8px;
          word-break: break-all; margin-bottom: 8px; user-select: all; }}
  button {{ font-size: 16px; padding: 12px 20px; border-radius: 8px;
            border: 1px solid rgba(127,127,127,0.3); background: #007aff;
            color: white; font-weight: 600; cursor: pointer; width: 100%;
            margin-bottom: 16px; }}
  button.copied {{ background: #34c759; }}
  ol {{ padding-left: 20px; }}
  code {{ background: rgba(127,127,127,0.15); padding: 2px 6px;
          border-radius: 4px; font-size: 90%; }}
  .footer {{ margin-top: 32px; font-size: 12px; color: #888; }}
</style>
</head><body>
<h1>Deja — mobile key for <em>{safe_label}</em></h1>
<div class="label">API key (tap to copy)</div>
<div class="key" id="k">{safe_k}</div>
<button id="copy" onclick="copyKey()">Copy key</button>
<ol>
  <li>Open <b>Shortcuts</b> on iPhone → create new Shortcut.</li>
  <li>Add <b>Dictate Text</b>.</li>
  <li>Add <b>Get Contents of URL</b>:
    <ul>
      <li>URL: <code>https://deja-api.onrender.com/v1/inbox</code></li>
      <li>Method: <b>POST</b></li>
      <li>Headers: <code>Content-Type: application/json</code>, <code>X-Deja-Mobile-Key: &lt;paste from above&gt;</code></li>
      <li>Body (JSON): <code>text</code> ← Dictated Text, <code>source</code> ← <code>ios-shortcut</code></li>
    </ul>
  </li>
  <li>Name it <b>"Note to Deja"</b>. Bind to Action Button / Back Tap / Siri.</li>
</ol>
<div class="footer">Key shown once. Keep secret. Revocation CLI coming.</div>
<script>
  function copyKey() {{
    const k = document.getElementById('k').textContent;
    navigator.clipboard.writeText(k).then(() => {{
      const b = document.getElementById('copy');
      b.textContent = 'Copied ✓';
      b.classList.add('copied');
      setTimeout(() => {{ b.textContent = 'Copy key'; b.classList.remove('copied'); }}, 2000);
    }});
  }}
</script>
</body></html>"""
    return HTMLResponse(html)


@app.post("/v1/inbox/keys")
@limiter.limit("20/hour")
async def mobile_key_create_endpoint(
    request: Request,
    authorization: str = Header(...),
):
    """Issue a long-lived mobile API key for iOS Shortcuts.

    Requires a live Google bearer (logged-in desktop app). Body may be
    empty; an optional ``{"label": "iphone"}`` JSON body labels the key
    so the user can distinguish between devices in case we add
    revocation UX later. The plaintext is returned ONCE; only the hash
    persists.
    """
    from db import mobile_key_create
    token = authorization.removeprefix("Bearer ").strip()
    user = await validate_token(token)
    label = "mobile"
    try:
        payload = await request.json()
        if isinstance(payload, dict) and payload.get("label"):
            label = str(payload["label"])[:40]
    except Exception:
        pass
    plaintext = mobile_key_create(user["email"], label)
    logger.info(
        "[rid=%s] mobile_key_create user=%s label=%s",
        request.state.rid, user["email"], label,
    )
    return {"key": plaintext, "label": label}


@app.post("/v1/inbox")
@limiter.limit("60/minute")
async def mobile_inbox_post(
    request: Request,
    x_deja_mobile_key: str | None = Header(None),
    authorization: str | None = Header(None),
):
    """Queue a mobile signal for the user's Deja to pick up.

    Auth accepts EITHER:
      - ``X-Deja-Mobile-Key: deja_...`` (preferred — long-lived, for iOS)
      - ``Authorization: Bearer <google-id-token>`` (desktop fallback)

    Body: ``{"text": "...", "source": "ios-shortcut|ios-screenshot|..."}``.
    Stored verbatim. Local Deja drains via ``/v1/inbox/drain`` every
    few seconds and routes each item into the cos command pipeline.
    """
    from db import mobile_key_lookup, mobile_inbox_put

    user_email: str | None = None
    if x_deja_mobile_key:
        user_email = mobile_key_lookup(x_deja_mobile_key.strip())
    if user_email is None and authorization and authorization.startswith("Bearer "):
        try:
            user = await validate_token(authorization.removeprefix("Bearer ").strip())
            user_email = user.get("email")
        except Exception:
            user_email = None
    if not user_email:
        raise HTTPException(status_code=401, detail="Unauthorized — missing or invalid mobile key")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    text = (body.get("text") or "").strip() if isinstance(body, dict) else ""
    source = (body.get("source") or "ios").strip()[:40] if isinstance(body, dict) else "ios"
    kind = (body.get("kind") or "note").strip()[:20] if isinstance(body, dict) else "note"
    if not text:
        raise HTTPException(status_code=400, detail="Missing text")
    if len(text) > 10_000:
        raise HTTPException(status_code=413, detail="Text too long (10k max)")

    item_id = mobile_inbox_put(user_email, source, kind, text)
    logger.info(
        "[rid=%s] inbox_put user=%s source=%s kind=%s chars=%d id=%d",
        request.state.rid, user_email, source, kind, len(text), item_id,
    )
    return {"ok": True, "id": item_id}


@app.post("/v1/inbox/drain")
@limiter.limit("120/minute")
async def mobile_inbox_drain(
    request: Request,
    authorization: str = Header(...),
):
    """Return and delete pending inbox items for the calling user.

    Uses Google bearer auth only (this is the desktop-side drain, not
    reached from mobile). Local Deja calls this every few seconds and
    pushes each item through ``chief_of_staff.invoke_command_sync``.
    """
    from db import mobile_inbox_drain as _drain
    token = authorization.removeprefix("Bearer ").strip()
    user = await validate_token(token)
    items = _drain(user["email"])
    if items:
        logger.info(
            "[rid=%s] inbox_drain user=%s n=%d",
            request.state.rid, user["email"], len(items),
        )
    return {"items": items}


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


MAX_DIAGNOSTIC_BYTES = 2 * 1024 * 1024  # 2MB — generous but bounded


@app.post("/v1/diagnostics")
@limiter.limit("10/minute")
async def upload_diagnostic(
    request: Request,
    body: DiagnosticRequest,
    authorization: str = Header(...),
):
    """Accept a diagnostic bundle from the desktop client and store it.

    Replaces the old mailto-based "Send Logs" flow — email is insecure
    and lossy. Clients bundle their logs, the server stores them keyed
    by a UUID + user email, and admins view them via
    ``/admin/diagnostic/{id}``. Returns ``{id}`` so the user can share
    the ID with support when filing a ticket.
    """
    import uuid
    token = authorization.removeprefix("Bearer ").strip()
    user = await validate_token(token)

    if len(body.bundle.encode("utf-8")) > MAX_DIAGNOSTIC_BYTES:
        raise HTTPException(status_code=413, detail="bundle too large")

    diag_id = uuid.uuid4().hex[:16]
    store_diagnostic(
        diag_id=diag_id,
        user_email=user.get("email"),
        client_version=body.client_version,
        note=body.note[:500],
        bundle=body.bundle,
    )
    logger.info(
        "[rid=%s] diagnostic stored id=%s user=%s size=%d",
        request.state.rid, diag_id, user.get("email"), len(body.bundle),
    )
    return {"id": diag_id}


# -- Admin Dashboard ----------------------------------------------------------

import base64
import hmac
import hashlib
import json as _json
import os
import time as _time
import html as _html
from fastapi import Cookie
from fastapi.responses import HTMLResponse, RedirectResponse
from db import search_events, get_stats, get_user_detail, store_diagnostic, list_diagnostics, get_diagnostic

def _esc(s) -> str:
    """HTML-escape user-controlled strings for safe rendering."""
    return _html.escape(str(s)) if s else ""


def _time_ago(iso_ts: str | None) -> str:
    """Render an ISO-8601 UTC timestamp as "Nm ago" / "Nh ago" / "Nd ago".

    Admin rows used to display raw UTC which was hard to parse at a
    glance. Everything in the DB is timezone-naive UTC; compare to
    naive UTC now and render the delta.
    """
    if not iso_ts:
        return "—"
    from datetime import datetime, timezone as _tz
    try:
        s = str(iso_ts).replace("Z", "")
        if "." in s:
            s = s.split(".", 1)[0]
        ts = datetime.fromisoformat(s)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=_tz.utc)
    except (ValueError, TypeError):
        return str(iso_ts)[:19]
    now = datetime.now(_tz.utc)
    delta = now - ts
    secs = int(delta.total_seconds())
    if secs < 0:
        return "just now"
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


# ---------------------------------------------------------------------------
# Admin auth — email-allowlisted cookie session.
#
# The legacy ``?key=`` query-param auth was a shared secret with no user
# attribution. The new flow:
#
#   1. Desktop app opens /admin/login?token=<user's existing Google OAuth
#      token> in the default browser. The token is validated with the same
#      code every /v1/* route uses.
#   2. /admin/login extracts the email, checks DEJA_ADMIN_EMAILS, and sets
#      a signed session cookie that carries email + expiry.
#   3. Every /admin/* route reads the cookie, verifies the HMAC, and
#      re-checks the email against DEJA_ADMIN_EMAILS on each request
#      (so revoking a user's admin access in Render takes effect
#      immediately, regardless of cookie validity).
# ---------------------------------------------------------------------------

SESSION_SECRET = os.environ.get("DEJA_SESSION_SECRET", "")
SESSION_COOKIE = "deja_admin_session"
SESSION_TTL_SECONDS = 7 * 24 * 3600  # 7 days

if not SESSION_SECRET:
    logger.warning(
        "DEJA_SESSION_SECRET not set — admin dashboard cookie auth disabled. "
        "Set to a random 32+ char string to enable."
    )


def _admin_emails() -> set[str]:
    """Parse the admin allowlist fresh each call so env changes take effect
    without a deploy. Comma-separated, case-insensitive matching."""
    raw = os.environ.get("DEJA_ADMIN_EMAILS", "")
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def _sign_session(email: str) -> str:
    """Return a signed cookie value that encodes ``email`` + expiry.

    Format: ``base64(payload).hex(hmac)``. Payload is JSON with email +
    exp timestamp so verification can reject expired cookies without
    DB state. HMAC uses SHA-256 over the raw payload bytes.
    """
    payload = _json.dumps({"email": email, "exp": int(_time.time()) + SESSION_TTL_SECONDS}, separators=(",", ":"))
    payload_b = payload.encode("utf-8")
    payload_b64 = base64.urlsafe_b64encode(payload_b).rstrip(b"=").decode("ascii")
    sig = hmac.new(SESSION_SECRET.encode(), payload_b, hashlib.sha256).hexdigest()
    return f"{payload_b64}.{sig}"


def _verify_session(cookie_value: str | None) -> str | None:
    """Return the email encoded in the cookie if the signature and expiry
    check out AND the email is still in DEJA_ADMIN_EMAILS. None otherwise.

    We re-check the allowlist on every request so removing someone from
    DEJA_ADMIN_EMAILS in Render takes effect immediately — their next
    request 403s even if their cookie is still valid.
    """
    if not cookie_value or not SESSION_SECRET:
        return None
    try:
        payload_b64, sig = cookie_value.split(".", 1)
    except ValueError:
        return None

    # Re-pad the base64 (we stripped "=" for cookie friendliness)
    padding = 4 - (len(payload_b64) % 4)
    if padding and padding < 4:
        payload_b64 += "=" * padding
    try:
        payload_b = base64.urlsafe_b64decode(payload_b64)
    except Exception:
        return None

    expected_sig = hmac.new(SESSION_SECRET.encode(), payload_b, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected_sig):
        return None

    try:
        data = _json.loads(payload_b.decode("utf-8"))
    except Exception:
        return None

    if int(data.get("exp", 0)) < _time.time():
        return None

    email = (data.get("email") or "").lower()
    if email not in _admin_emails():
        return None
    return email


def _require_admin(session_cookie: str | None) -> str | HTMLResponse:
    """Return the admin email if the cookie is valid; otherwise an
    HTMLResponse redirecting to /admin/login. Caller must short-circuit
    on HTMLResponse — admin routes do `if isinstance(...): return ...`.
    """
    email = _verify_session(session_cookie)
    if email:
        return email
    return HTMLResponse(
        '<h1>Not signed in</h1><p><a href="/admin/login">Sign in via Deja.app</a></p>',
        status_code=401,
    )


@app.get("/admin/login")
async def admin_login(token: str = ""):
    """Entry point for the desktop app → admin dashboard handoff.

    The desktop app opens this URL in the user's browser with the
    user's Google OAuth token as a query param. We validate the
    token via the same code path every /v1/* route uses, check the
    email against DEJA_ADMIN_EMAILS, and set a signed session cookie.
    The cookie is HttpOnly + SameSite=Lax so the query-param token
    only lives in history for one redirect, not in a durable cookie.
    """
    if not token:
        return HTMLResponse(
            '<h1>Missing token</h1>'
            '<p>Open the admin dashboard from inside Deja.app — '
            'the desktop client passes your session token here.</p>',
            status_code=400,
        )
    try:
        user = await validate_token(token)
    except HTTPException as e:
        return HTMLResponse(f"<h1>Invalid token</h1><p>{_esc(e.detail)}</p>", status_code=401)

    email = (user.get("email") or "").lower()
    if email not in _admin_emails():
        logger.warning("admin login denied for %s", email)
        return HTMLResponse(
            f'<h1>Not authorized</h1>'
            f'<p>{_esc(email)} is not in the admin allowlist.</p>',
            status_code=403,
        )

    if not SESSION_SECRET:
        return HTMLResponse(
            "<h1>Dashboard disabled</h1>"
            "<p>DEJA_SESSION_SECRET is not set on the server. "
            "Ask an operator to configure it in Render env vars.</p>",
            status_code=503,
        )

    cookie_value = _sign_session(email)
    response = RedirectResponse(url="/admin", status_code=303)
    response.set_cookie(
        key=SESSION_COOKIE,
        value=cookie_value,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/admin",
    )
    return response


@app.get("/admin/logout")
async def admin_logout():
    response = HTMLResponse(
        '<h1>Signed out</h1>'
        '<p><a href="/admin/login">Sign in again</a></p>'
    )
    response.delete_cookie(SESSION_COOKIE, path="/admin")
    return response


@app.get("/admin")
async def admin_dashboard(deja_admin_session: str | None = Cookie(default=None)):
    gate = _require_admin(deja_admin_session)
    if isinstance(gate, HTMLResponse):
        return gate
    admin_email = gate

    stats = get_stats()
    errors_html = ""
    for e in stats["recent_errors"]:
        props = _esc(e.get("properties", "{}"))
        errors_html += (
            f"<tr><td title=\"{_esc(e['timestamp'])}\">{_esc(_time_ago(e['timestamp']))}</td>"
            f"<td>{_esc(e['user_email']) or '—'}</td><td>{props}</td></tr>"
        )

    users_html = ""
    for u in stats["active_users"]:
        email = u["user_email"]
        last = u["last_seen"]
        users_html += (
            f"<tr><td><a href=\"/admin/user?email={_esc(email)}\">{_esc(email)}</a></td>"
            f"<td>{u['event_count']}</td>"
            f"<td title=\"{_esc(last)}\">{_esc(_time_ago(last))}</td></tr>"
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
<div style="display:flex;align-items:baseline;gap:16px;">
  <h1 style="margin:0;">Déjà Admin</h1>
  <div style="color:#888;font-size:12px;flex:1;">Signed in as <strong>{_esc(admin_email)}</strong></div>
  <a href="/admin/diagnostics" style="color:#888;font-size:12px;">Diagnostics</a>
  <a href="/admin/logout" style="color:#888;font-size:12px;">Sign out</a>
</div>

<div class="stats">
  <div class="stat"><div class="stat-num">{stats['unique_users']}</div><div class="stat-label">Users</div></div>
  <div class="stat"><div class="stat-num">{stats['total_events']}</div><div class="stat-label">Events</div></div>
  <div class="stat"><div class="stat-num error">{stats['total_errors']}</div><div class="stat-label">Errors</div></div>
</div>

<h2>Search</h2>
<form action="/admin/search" method="get">
  <input type="text" name="q" placeholder="Request ID, email, or keyword...">
  <button type="submit">Search</button>
</form>

<h2>Active Users</h2>
<table><tr><th>Email</th><th>Events</th><th>Last Seen</th></tr>{users_html}</table>

<h2>Recent Errors</h2>
<table><tr><th>Time</th><th>User</th><th>Details</th></tr>{errors_html}</table>

</body></html>""")


@app.get("/admin/search")
async def admin_search(
    q: str = "",
    event: str = "",
    deja_admin_session: str | None = Cookie(default=None),
):
    gate = _require_admin(deja_admin_session)
    if isinstance(gate, HTMLResponse):
        return gate

    results = search_events(query=q, event_type=event, limit=200)

    rows_html = ""
    for r in results:
        rows_html += (
            f"<tr><td title=\"{_esc(r['timestamp'])}\">{_esc(_time_ago(r['timestamp']))}</td>"
            f"<td>{_esc(r['event'])}</td>"
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
<h1><a href="/admin">← Déjà Admin</a> — Search: "{_esc(q or event)}"</h1>

<form action="/admin/search" method="get">
  <input type="text" name="q" placeholder="Request ID, email, or keyword..." value="{_esc(q)}">
  <button type="submit">Search</button>
</form>

<p>{len(results)} result(s)</p>
<table><tr><th>Time</th><th>Event</th><th>User</th><th>Request ID</th><th>Details</th></tr>{rows_html}</table>

</body></html>""")


@app.get("/admin/user")
async def admin_user_detail(
    email: str = "",
    deja_admin_session: str | None = Cookie(default=None),
):
    """Per-user drill-down — aggregate counts, event breakdown, timeline.

    Accessed by clicking an email on the main dashboard. Shows everything
    the proxy knows about one user without requiring a free-text search.
    """
    gate = _require_admin(deja_admin_session)
    if isinstance(gate, HTMLResponse):
        return gate
    if not email:
        return HTMLResponse("<h1>Missing email</h1>", status_code=400)

    detail = get_user_detail(email, limit=100)
    profile = detail["profile"]

    if not profile or not profile.get("total_events"):
        return HTMLResponse(f"<h1>No events for {_esc(email)}</h1>", status_code=404)

    first_seen_raw = profile.get("first_seen", "")
    last_seen_raw = profile.get("last_seen", "")
    first_seen = _time_ago(first_seen_raw)
    last_seen = _time_ago(last_seen_raw)

    breakdown_html = ""
    for row in detail["event_breakdown"]:
        breakdown_html += f"<tr><td>{_esc(row['event'])}</td><td>{row['count']}</td></tr>"

    timeline_html = ""
    for e in detail["recent_events"]:
        timeline_html += (
            f"<tr><td title=\"{_esc(e['timestamp'])}\">{_esc(_time_ago(e['timestamp']))}</td>"
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
<h1><a href="/admin">← Déjà Admin</a> — {_esc(email)}</h1>
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


@app.get("/admin/diagnostics")
async def admin_diagnostics_list(
    email: str = "",
    deja_admin_session: str | None = Cookie(default=None),
):
    """List uploaded diagnostic bundles, optionally filtered by user."""
    gate = _require_admin(deja_admin_session)
    if isinstance(gate, HTMLResponse):
        return gate

    rows = list_diagnostics(limit=200, email=email or None)
    rows_html = ""
    for r in rows:
        size_kb = int((r.get("size_bytes") or 0) / 1024)
        rows_html += (
            f"<tr><td title=\"{_esc(r['timestamp'])}\">{_esc(_time_ago(r['timestamp']))}</td>"
            f"<td><a href=\"/admin/user?email={_esc(r['user_email'] or '')}\">{_esc(r['user_email']) or '—'}</a></td>"
            f"<td>{_esc(r.get('client_version'))}</td>"
            f"<td>{size_kb} KB</td>"
            f"<td>{_esc((r.get('note') or '')[:80])}</td>"
            f"<td><a href=\"/admin/diagnostic/{_esc(r['id'])}\">{_esc(r['id'])}</a></td></tr>"
        )

    heading = f" — {_esc(email)}" if email else ""
    return HTMLResponse(f"""<!DOCTYPE html>
<html><head><title>Déjà Admin — Diagnostics</title>
<style>
  body {{ font-family: -apple-system, sans-serif; max-width: 1100px; margin: 40px auto; background: #111; color: #eee; padding: 0 20px; }}
  h1 {{ color: #fff; }} a {{ color: #8cf; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 8px; }}
  th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid #333; font-size: 12px; }}
  th {{ color: #888; font-weight: 600; }}
</style></head><body>
<h1><a href="/admin">← Déjà Admin</a> — Diagnostics{heading}</h1>
<p>{len(rows)} bundle(s)</p>
<table><tr><th>Time</th><th>User</th><th>Client</th><th>Size</th><th>Note</th><th>ID</th></tr>{rows_html}</table>
</body></html>""")


@app.get("/admin/diagnostic/{diag_id}")
async def admin_diagnostic_detail(
    diag_id: str,
    deja_admin_session: str | None = Cookie(default=None),
):
    """Render one uploaded diagnostic bundle as plain text."""
    gate = _require_admin(deja_admin_session)
    if isinstance(gate, HTMLResponse):
        return gate

    diag = get_diagnostic(diag_id)
    if not diag:
        return HTMLResponse(f"<h1>Not found: {_esc(diag_id)}</h1>", status_code=404)

    return HTMLResponse(f"""<!DOCTYPE html>
<html><head><title>Déjà Diagnostic {_esc(diag_id)}</title>
<style>
  body {{ font-family: -apple-system, sans-serif; max-width: 1100px; margin: 40px auto; background: #111; color: #eee; padding: 0 20px; }}
  h1 {{ color: #fff; }} a {{ color: #8cf; }}
  .meta {{ color: #888; font-size: 13px; margin: 8px 0 20px; }}
  pre {{ background: #1a1a1a; padding: 16px; border-radius: 6px; font-size: 11px; line-height: 1.4; overflow-x: auto; white-space: pre-wrap; word-break: break-all; }}
</style></head><body>
<h1><a href="/admin/diagnostics">← Diagnostics</a> — {_esc(diag_id)}</h1>
<div class="meta">
  User: <strong>{_esc(diag.get('user_email')) or '—'}</strong> ·
  Client: {_esc(diag.get('client_version'))} ·
  Uploaded: <span title="{_esc(diag.get('timestamp', ''))}">{_esc(_time_ago(diag.get('timestamp', '')))}</span> ·
  Size: {int((diag.get('size_bytes') or 0)/1024)} KB
</div>
{f'<p><strong>Note:</strong> {_esc(diag.get("note"))}</p>' if diag.get('note') else ''}
<pre>{_esc(diag.get('bundle', ''))}</pre>
</body></html>""")
