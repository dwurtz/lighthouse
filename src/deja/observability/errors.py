"""Typed error hierarchy for Deja.

Every user-visible failure — and every internal failure that we want to
trace — is raised as a subclass of :class:`DejaError`. Each carries:

* ``code`` — a stable machine-readable string (``proxy_unavailable``,
  ``auth_failed``, ...). UI surfaces branch on this, not on wording.
* ``user_message`` — a friendly single-sentence default the UI can show
  verbatim when it doesn't know better.
* ``details`` — arbitrary dict of debugging info (HTTP status, URL,
  upstream error text). Never shown to the user directly.
* ``request_id`` — snapshot of the active request id at raise time, so
  the exception can be reported from anywhere without threading context
  through every caller.

The ``to_user_error_file()`` helper materializes the contract shape
that the Swift UI reads from ``~/.deja/latest_error.json``.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from deja.observability.context import current_request_id


def _utc_now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _latest_error_path() -> Path:
    # Resolved lazily so tests that monkeypatch ``DEJA_HOME`` see the
    # right value.
    from deja.config import DEJA_HOME
    return Path(DEJA_HOME) / "latest_error.json"


class DejaError(Exception):
    """Base class for all Deja-typed errors."""

    code: str = "deja_error"
    default_user_message: str = "Something went wrong."

    def __init__(
        self,
        message: str | None = None,
        *,
        user_message: str | None = None,
        details: dict[str, Any] | None = None,
        code: str | None = None,
    ) -> None:
        super().__init__(message or user_message or self.default_user_message)
        self.user_message: str = user_message or self.default_user_message
        self.details: dict[str, Any] = dict(details or {})
        if code:
            self.code = code
        self.request_id: str | None = current_request_id()

    def to_payload(self) -> dict[str, Any]:
        """Return the on-disk JSON shape used by both sinks."""
        return {
            "request_id": self.request_id,
            "code": self.code,
            "message": self.user_message,
            "timestamp": _utc_now_iso(),
            "details": self.details,
        }

    def to_user_error_file(self, path: Path | None = None) -> Path:
        """Atomically write the payload to ``~/.deja/latest_error.json``.

        Temp file + ``os.replace`` guarantees Swift never reads a half-
        written document. Returns the final path for tests.
        """
        target = path or _latest_error_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = self.to_payload()
        fd, tmp_path = tempfile.mkstemp(
            prefix=".latest_error.", suffix=".tmp", dir=str(target.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f)
            os.replace(tmp_path, target)
        except Exception:
            try:
                os.unlink(tmp_path)
            except FileNotFoundError:
                pass
            raise
        return target


class ProxyUnavailable(DejaError):
    """Deja's LLM proxy is unreachable: 502/503/504, timeout, connect error."""

    code = "proxy_unavailable"
    default_user_message = (
        "Couldn't reach Deja's LLM server. It may be briefly restarting — "
        "retry in a moment."
    )


class AuthError(DejaError):
    """Authentication / authorization failed (401, 403)."""

    code = "auth_failed"
    default_user_message = (
        "Deja couldn't authenticate with the server. Re-run setup to "
        "reconnect your account."
    )


class RateLimitError(DejaError):
    """Upstream rate limit hit (429)."""

    code = "rate_limited"
    default_user_message = (
        "Deja is being rate-limited right now. Try again in a minute."
    )


class LLMError(DejaError):
    """Generic 5xx from the LLM (not a proxy outage — Gemini itself failed)."""

    code = "llm_error"
    default_user_message = (
        "The language model returned an error. Try again in a moment."
    )


class ConfigError(DejaError):
    """Missing or invalid local configuration (wiki, identity, goals)."""

    code = "config_error"
    default_user_message = (
        "Deja's configuration is incomplete. Open the app and re-run setup."
    )


class ToolError(DejaError):
    """Subprocess tool failure (gws, qmd, ocr, ffmpeg, ...)."""

    code = "tool_error"
    default_user_message = (
        "One of Deja's tools failed to run. Check the logs for details."
    )


__all__ = [
    "DejaError",
    "ProxyUnavailable",
    "AuthError",
    "RateLimitError",
    "LLMError",
    "ConfigError",
    "ToolError",
]
