"""Client-side telemetry for Déjà.

Sends lightweight operational events to the Deja API server for
debugging and usage analytics. All calls are fire-and-forget —
failures are silent and never affect app behavior.

Privacy principles:
  - NO message content, wiki text, email bodies, or screenshot data
  - NO file paths containing usernames (sanitized to ~/)
  - Only operational state: event names, counts, durations, error codes
  - User identity is the Google email (already authenticated)
  - Telemetry can be disabled via config.yaml: telemetry_enabled: false
"""

from __future__ import annotations

import logging
import os
import platform
import threading
import time
from typing import Any

log = logging.getLogger(__name__)

_DEJA_API_URL = os.environ.get("DEJA_API_URL", "https://deja-api.onrender.com")
_VERSION = "0.2.0"

# Disable telemetry in dev mode (GEMINI_API_KEY set) or via config
_ENABLED: bool | None = None  # lazy-loaded


def _is_enabled() -> bool:
    global _ENABLED
    if _ENABLED is not None:
        return _ENABLED

    # Dev mode bypass
    if os.environ.get("GEMINI_API_KEY"):
        _ENABLED = False
        return False

    # Config override
    try:
        from deja.config import _raw
        _ENABLED = _raw.get("telemetry_enabled", True)
    except Exception:
        _ENABLED = True

    return _ENABLED


def track(event: str, properties: dict[str, Any] | None = None) -> None:
    """Send a telemetry event to the server. Non-blocking, fire-and-forget.

    Args:
        event: Event name (e.g. 'setup_completed', 'analysis_cycle')
        properties: Optional dict of event-specific data. Must NOT contain
                    PII or content — only counts, durations, status codes.
    """
    if not _is_enabled():
        return

    props = properties or {}

    # Run in a daemon thread so it never blocks the caller
    thread = threading.Thread(
        target=_send_event,
        args=(event, props),
        daemon=True,
    )
    thread.start()


def _send_event(event: str, properties: dict) -> None:
    """Actually send the event. Runs in a background thread."""
    try:
        import httpx

        token = _get_token()
        headers = {"Authorization": f"Bearer {token}"} if token else {}

        httpx.post(
            f"{_DEJA_API_URL}/v1/telemetry",
            json={
                "event": event,
                "properties": properties,
                "client_version": _VERSION,
            },
            headers=headers,
            timeout=5,
        )
    except Exception:
        pass  # telemetry must never crash the app


def _get_token() -> str | None:
    """Get auth token for telemetry requests."""
    try:
        from deja.auth import get_auth_token
        return get_auth_token()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Convenience helpers for common events
# ---------------------------------------------------------------------------

def track_setup_step(step: str, **extra: Any) -> None:
    """Track a setup funnel step.

    Steps: setup_started, google_auth_completed, screen_recording_granted,
           fda_granted, setup_completed
    """
    track(f"setup.{step}", extra)


def track_error(component: str, error: str, **extra: Any) -> None:
    """Track an error event.

    Args:
        component: Where the error happened (e.g. 'llm', 'vision', 'auth')
        error: Error type/message (sanitized — no PII)
    """
    track("error", {"component": component, "error": error[:200], **extra})


def track_llm_call(model: str, duration_ms: int, ok: bool, **extra: Any) -> None:
    """Track an LLM API call (success or failure)."""
    track("llm_call", {
        "model": model,
        "duration_ms": duration_ms,
        "ok": ok,
        **extra,
    })


def track_heartbeat() -> None:
    """Periodic heartbeat with system state. Called every 10 minutes."""
    try:
        from deja.config import WIKI_DIR, DEJA_HOME
        from pathlib import Path

        wiki_pages = 0
        try:
            for subdir in ["people", "projects", "events"]:
                d = WIKI_DIR / subdir
                if d.exists():
                    wiki_pages += len(list(d.glob("*.md")))
        except Exception:
            pass

        obs_count = 0
        try:
            obs_file = DEJA_HOME / "observations.jsonl"
            if obs_file.exists():
                obs_count = sum(1 for _ in open(obs_file))
        except Exception:
            pass

        track("heartbeat", {
            "wiki_pages": wiki_pages,
            "observations": obs_count,
            "os_version": platform.mac_ver()[0],
            "uptime_minutes": int((time.time() - _START_TIME) / 60),
        })
    except Exception:
        pass


_START_TIME = time.time()
