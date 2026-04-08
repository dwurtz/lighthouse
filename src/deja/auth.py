"""Google OAuth token for Deja API server authentication.

The token is obtained during setup via `gws auth login` and stored
by the gws CLI. We read it from gws's token file to attach to
server requests.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

_TOKEN_PATH = Path.home() / ".config" / "gws" / "token.json"


def _read_token_file() -> dict | None:
    """Read and return the parsed gws token.json, or None."""
    try:
        if not _TOKEN_PATH.exists():
            return None
        return json.loads(_TOKEN_PATH.read_text())
    except (json.JSONDecodeError, OSError) as e:
        log.debug("Failed to read token file: %s", e)
        return None


def _is_expired(token_data: dict) -> bool:
    """Check whether the access token has expired."""
    import time

    expiry = token_data.get("expiry") or token_data.get("token_expiry")
    if not expiry:
        return True
    try:
        from datetime import datetime, timezone

        # Handle ISO-format and epoch-seconds expiry values
        if isinstance(expiry, (int, float)):
            return time.time() >= expiry
        dt = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
        return datetime.now(timezone.utc) >= dt
    except Exception:
        return True


def _refresh_token() -> bool:
    """Ask gws to refresh the OAuth token. Returns True on success."""
    try:
        r = subprocess.run(
            ["gws", "auth", "refresh"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        log.debug("Token refresh failed: %s", e)
        return False


def get_auth_token() -> str | None:
    """Return a valid Google OAuth token string, or None if not authenticated.

    Reads the gws CLI token file at ``~/.config/gws/token.json``.
    If the token is expired, attempts a refresh via ``gws auth refresh``.
    Returns the id_token if present, otherwise the access_token.
    """
    token_data = _read_token_file()
    if not token_data:
        return None

    if _is_expired(token_data):
        if not _refresh_token():
            log.warning("OAuth token expired and refresh failed")
            return None
        # Re-read after refresh
        token_data = _read_token_file()
        if not token_data:
            return None

    # Prefer id_token (contains user identity claims), fall back to access_token
    return token_data.get("id_token") or token_data.get("access_token") or token_data.get("token")


def get_user_email() -> str:
    """Extract the user's email from the token file or gws auth status.

    Returns an empty string if the email cannot be determined.
    """
    # Try token file first
    token_data = _read_token_file()
    if token_data:
        email = token_data.get("user") or token_data.get("email")
        if email:
            return email

    # Fall back to gws auth status
    try:
        r = subprocess.run(
            ["gws", "auth", "status"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if r.returncode == 0:
            status = json.loads(r.stdout)
            return status.get("user", "")
    except Exception:
        pass

    return ""
