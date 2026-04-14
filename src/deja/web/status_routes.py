"""GET /api/status — liveness probe.
GET /api/activity — recent activity feed for the notch popover."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from pydantic import BaseModel

from deja import audit
from deja.web.helpers import OBSERVATIONS_LOG

router = APIRouter()

# Lightweight in-memory call tracker used by /api/debug/state. Each
# entry is the most recent invocation of an endpoint with timestamp +
# response summary, so a single GET /api/debug/state answers
# "what does the backend think happened?" without log scraping.
_LAST_CALLS: dict[str, dict] = {}


def _record_call(name: str, **fields) -> None:
    _LAST_CALLS[name] = {
        "at": datetime.now(timezone.utc).isoformat(),
        **fields,
    }


@router.get("/api/status")
def get_status() -> dict:
    """Return liveness info. ``monitor_running`` is true if a signal landed in
    the last 120s."""
    last_signal_time = None
    if OBSERVATIONS_LOG.exists():
        with open(OBSERVATIONS_LOG, "rb") as f:
            try:
                f.seek(-4096, 2)
            except OSError:
                f.seek(0)
            tail = f.read().decode("utf-8", errors="replace").splitlines()
        for line in reversed(tail):
            line = line.strip()
            if not line:
                continue
            try:
                last_signal_time = json.loads(line).get("timestamp")
                break
            except json.JSONDecodeError:
                continue

    last_analysis_time = None
    if audit.AUDIT_LOG.exists():
        with open(audit.AUDIT_LOG, "rb") as f:
            try:
                f.seek(-4096, 2)
            except OSError:
                f.seek(0)
            tail = f.read().decode("utf-8", errors="replace").splitlines()
        for line in reversed(tail):
            line = line.strip()
            if not line:
                continue
            try:
                last_analysis_time = json.loads(line).get("ts")
                break
            except json.JSONDecodeError:
                continue

    monitor_running = False
    if last_signal_time:
        last_dt = datetime.fromisoformat(last_signal_time.replace("Z", "+00:00"))
        if last_dt.tzinfo is None:
            last_dt = last_dt.astimezone()
        age = (datetime.now(timezone.utc) - last_dt).total_seconds()
        monitor_running = age < 120

    # Screen Recording permission check
    screen_recording = True
    try:
        from deja.observations.screenshot import screen_recording_granted

        screen_recording = screen_recording_granted()
    except Exception:
        pass

    return {
        "monitor_running": monitor_running,
        "last_signal_time": last_signal_time,
        "last_analysis_time": last_analysis_time,
        "screen_recording": screen_recording,
    }


@router.get("/api/admin-dashboard-url")
def admin_dashboard_url(request: Request) -> dict:
    """Build the one-shot admin login URL for the Swift app to open.

    The URL embeds the user's current OAuth token as a query param so
    the server can validate it via the same code path every /v1/*
    route uses, then set a signed cookie + redirect to /admin. The
    token only lives in browser history for one redirect; the cookie
    is HttpOnly and what persists.

    Returns ``{"url": "..."}`` on success or ``{"error": "..."}`` if
    the user isn't signed in or the proxy URL isn't configured.
    """
    try:
        from urllib.parse import quote
        from deja.auth import get_auth_token
        from deja.llm_client import DEJA_API_URL

        rid = request.headers.get("x-deja-request-id")
        token = get_auth_token()
        if not token:
            _record_call("admin_dashboard_url", result="not_signed_in", rid=rid)
            return {"error": "not signed in"}
        _record_call("admin_dashboard_url", result="ok", rid=rid)
        return {"url": f"{DEJA_API_URL}/admin/login?token={quote(token)}"}
    except Exception as e:
        _record_call("admin_dashboard_url", result="error", error=str(e)[:200], rid=request.headers.get("x-deja-request-id"))
        return {"error": str(e)[:200]}


@router.get("/api/me")
async def whoami(request: Request) -> dict:
    """Identity + admin check — proxies to server /v1/me.

    Used by the Swift app to decide whether to show the "Open Admin
    Dashboard" tray menu item. Non-admin accounts get ``is_admin:
    false`` and the menu item stays hidden. If the user isn't signed
    in (no token) or the server is unreachable, returns a safe
    default with ``is_admin: false``.
    """
    try:
        import httpx
        from deja.auth import get_auth_token
        from deja.llm_client import DEJA_API_URL

        rid = request.headers.get("x-deja-request-id")
        token = get_auth_token()
        if not token:
            _record_call("me", result="not_signed_in", rid=rid)
            return {"email": "", "is_admin": False, "signed_in": False}

        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                f"{DEJA_API_URL}/v1/me",
                headers={"Authorization": f"Bearer {token}"},
            )
        if 200 <= resp.status_code < 300:
            data = resp.json()
            out = {
                "email": data.get("email", ""),
                "is_admin": bool(data.get("is_admin", False)),
                "signed_in": True,
            }
            _record_call("me", result="ok", email=out["email"], is_admin=out["is_admin"], rid=rid)
            return out
        _record_call("me", result="http_error", status=resp.status_code, rid=rid)
        return {"email": "", "is_admin": False, "signed_in": False, "error": f"HTTP {resp.status_code}"}
    except Exception as e:
        _record_call("me", result="exception", error=str(e)[:120], rid=request.headers.get("x-deja-request-id"))
        return {"email": "", "is_admin": False, "signed_in": False, "error": str(e)[:120]}


@router.get("/api/briefing")
def get_briefing() -> dict:
    """Return the 'right now' briefing for the notch panel.

    Deterministic — no LLM, no retrieval. Pure derivation from goals.md
    (tasks with deadlines, stale waiting-fors, due reminders) so the
    panel can render in milliseconds and refresh on a 30s poll.
    """
    from deja.briefing import build_briefing
    return build_briefing()


@router.get("/api/activity")
def get_activity(limit: int = 50) -> dict:
    """Return the most recent audit entries for the notch Activity tab.

    Reads ``~/.deja/audit.jsonl`` — one line per discrete agent action
    — and projects it into the ``{timestamp, kind, summary}`` shape the
    Swift notch UI expects. ``kind`` is the audit ``action`` field
    (wiki_write, reminder_resolve, etc.) and ``summary`` is derived
    from ``target`` + ``reason``.
    """
    raw = audit.read_recent(limit=limit)
    entries: list[dict] = []
    for e in raw:
        ts_iso = e.get("ts", "")
        # Convert ISO8601 to "YYYY-MM-DD HH:MM" local for UI rendering.
        ts_short = ts_iso[:16].replace("T", " ") if ts_iso else ""
        action = e.get("action", "")
        target = e.get("target", "")
        reason = e.get("reason", "")
        summary = f"{target} — {reason}" if target else reason
        entries.append(
            {
                "timestamp": ts_short,
                "kind": action,
                "summary": summary,
            }
        )
    return {"entries": entries}


@router.get("/api/debug/state")
def debug_state() -> dict:
    """One-shot snapshot of what the backend knows right now.

    Designed for remote debugging: a single GET answers questions like
    "did the menubar app ever call /api/me?", "what did the server say
    when it did?", "is the auth token even present?", and "what are
    the most recent Swift-side log lines?". Avoids forcing a user to
    upload a 100KB diagnostic just to check one fact.
    """
    from deja.auth import get_auth_token
    from deja.config import DEJA_HOME

    token = None
    auth_error = None
    try:
        token = get_auth_token()
    except Exception as e:
        auth_error = str(e)[:200]

    swift_lines: list[str] = []
    log_path = DEJA_HOME / "deja.log"
    if log_path.exists():
        try:
            with open(log_path, "r", errors="replace") as f:
                # Read last ~64KB and keep [swift]-tagged lines.
                try:
                    f.seek(-65536, 2)
                except OSError:
                    f.seek(0)
                tail = f.read().splitlines()
            swift_lines = [ln for ln in tail if "[swift]" in ln][-50:]
        except Exception as e:
            swift_lines = [f"(read error: {e})"]

    recent_errors: list[dict] = []
    err_path = DEJA_HOME / "errors.jsonl"
    if err_path.exists():
        try:
            with open(err_path, "r", errors="replace") as f:
                lines = f.readlines()[-10:]
            for ln in lines:
                try:
                    recent_errors.append(json.loads(ln))
                except json.JSONDecodeError:
                    pass
        except Exception as e:
            recent_errors = [{"error": str(e)}]

    swift_state = None
    swift_state_error = None
    sstate_path = DEJA_HOME / "swift_state.json"
    if sstate_path.exists():
        try:
            swift_state = json.loads(sstate_path.read_text(errors="replace"))
        except Exception as e:
            swift_state_error = str(e)[:200]
    else:
        swift_state_error = "missing"

    return {
        "now": datetime.now(timezone.utc).isoformat(),
        "auth": {
            "token_present": bool(token),
            "token_prefix": (token[:8] + "…") if token else None,
            "error": auth_error,
        },
        "last_calls": _LAST_CALLS,
        "swift_log_tail": swift_lines,
        "recent_errors": recent_errors,
        "swift_state": swift_state,
        "swift_state_error": swift_state_error,
    }


def _trim_tracebacks(text: str) -> str:
    """Collapse Python traceback line runs into a single annotated line.

    A traceback block starts at a line equal to ``Traceback (most recent call last):``
    and continues while subsequent lines are indented (start with whitespace).
    The block ends at the next non-indented line, which is the exception
    summary and is kept as the anchor. Output: ``<exception line> [+N traceback lines]``.
    """
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    i = 0
    n = len(lines)
    tb_re = re.compile(r"^Traceback \(most recent call last\):")
    while i < n:
        line = lines[i]
        # Strip timestamp/prefix? Just look at the raw line.
        if tb_re.search(line):
            start = i
            i += 1
            # Consume indented frames.
            while i < n and (lines[i].startswith(" ") or lines[i].startswith("\t")):
                i += 1
            # Next line (if any) is the exception summary.
            exc_line = lines[i].rstrip("\n") if i < n else lines[start].rstrip("\n")
            collapsed = (i - start)  # number of traceback lines collapsed (incl. header)
            if i < n:
                i += 1
            newline = "\n" if (out or i < n) else ""
            out.append(f"{exc_line} [+{collapsed} traceback lines]{newline}")
            continue
        out.append(line)
        i += 1
    return "".join(out)


class DiagnosticUploadRequest(BaseModel):
    note: str = ""


@router.post("/api/diagnostics/upload")
async def upload_diagnostics(body: DiagnosticUploadRequest) -> dict:
    """Bundle local logs and ship them to the Deja proxy for support.

    Replaces the old ``mailto:`` flow. Bundles:
      * last 500 lines of ``~/.deja/deja.log``
      * full ``~/.deja/errors.jsonl``
      * last 200 lines of ``~/.deja/audit.jsonl``
      * Swift side logs via ``log show --process Deja --last 10m``
      * system + app metadata

    POSTs the concatenated text bundle to ``{DEJA_API_URL}/v1/diagnostics``
    authenticated with the user's OAuth token. Returns ``{"id": ...}``
    which the user shares with support to look up the bundle via the
    admin dashboard.
    """
    import platform
    import httpx
    from deja.auth import get_auth_token
    from deja.config import DEJA_HOME
    from deja.llm_client import DEJA_API_URL

    def _tail(path, n):
        try:
            if not path.exists():
                return f"(missing: {path})"
            with open(path, "r", errors="replace") as f:
                lines = f.readlines()
            return "".join(lines[-n:])
        except Exception as e:
            return f"(read error: {e})"

    def _full(path, max_bytes=200_000):
        try:
            if not path.exists():
                return f"(missing: {path})"
            data = path.read_text(errors="replace")
            if len(data) > max_bytes:
                data = "(truncated head)\n" + data[-max_bytes:]
            return data
        except Exception as e:
            return f"(read error: {e})"

    parts: list[str] = []
    parts.append("=== metadata ===")
    parts.append(f"now: {datetime.now(timezone.utc).isoformat()}")
    parts.append(f"platform: {platform.platform()}")
    parts.append(f"python: {platform.python_version()}")
    if body.note:
        parts.append(f"note: {body.note}")
    parts.append("")

    parts.append("=== debug state ===")
    try:
        parts.append(json.dumps(debug_state(), indent=2, default=str))
    except Exception as e:
        parts.append(f"(debug_state error: {e})")
    parts.append("")

    parts.append("=== swift state ===")
    sstate_path = DEJA_HOME / "swift_state.json"
    if sstate_path.exists():
        try:
            parts.append(sstate_path.read_text(errors="replace"))
        except Exception as e:
            parts.append(f"(read error: {e})")
    else:
        parts.append("(missing)")
    parts.append("")

    parts.append("=== deja.log (last 500 lines, tracebacks collapsed) ===")
    parts.append(_trim_tracebacks(_tail(DEJA_HOME / "deja.log", 500)))
    parts.append("")

    parts.append("=== errors.jsonl ===")
    parts.append(_full(DEJA_HOME / "errors.jsonl"))
    parts.append("")

    parts.append("=== audit.jsonl (last 200 lines) ===")
    parts.append(_tail(DEJA_HOME / "audit.jsonl", 200))
    parts.append("")

    # Swift-side log lines are already appended to ~/.deja/deja.log via
    # swiftLog() in the menubar app, so no need to scrape `log show` and
    # ship megabytes of Apple framework noise.

    bundle = "\n".join(parts)
    # Cap to server max (2MB) minus a safety margin.
    max_chars = 1_900_000
    if len(bundle) > max_chars:
        bundle = bundle[-max_chars:]

    token = get_auth_token()
    if not token:
        return {"error": "not signed in"}

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{DEJA_API_URL}/v1/diagnostics",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "bundle": bundle,
                    "note": body.note,
                    "client_version": "0.2.0",
                },
            )
        if 200 <= resp.status_code < 300:
            return resp.json()
        return {"error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
    except Exception as e:
        return {"error": str(e)[:300]}
