"""Google OAuth for Deja API server authentication.

Uses google-auth-oauthlib for native OAuth — no external CLI dependency.
Tokens are stored at ~/.deja/google_token.json. Falls back to the legacy
gws token file for backwards compatibility.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

_DEJA_TOKEN_PATH = Path.home() / ".deja" / "google_token.json"
_GWS_TOKEN_PATH = Path.home() / ".config" / "gws" / "token.json"

# Scopes for Google Workspace — read for observation, write for actions
SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/gmail.modify",           # read + draft/send
    "https://www.googleapis.com/auth/calendar",                # read + create/edit events
    "https://www.googleapis.com/auth/drive",                   # read + create/edit files
    "https://www.googleapis.com/auth/tasks",                   # read + create/edit tasks
    "https://www.googleapis.com/auth/contacts",                # read + create/edit contacts
]


def _find_client_secret() -> Path | None:
    """Locate client_secret.json from bundled locations."""
    candidates = [
        Path.home() / ".config" / "gws" / "client_secret.json",
        Path(__file__).parent / "default_assets" / "client_secret.json",
    ]
    # macOS .app bundle
    if getattr(sys, "frozen", False):
        candidates.append(
            Path(sys.executable).parent.parent / "Resources" / "client_secret.json"
        )
    else:
        candidates.append(
            Path(__file__).parents[3] / "Resources" / "client_secret.json"
        )
    for p in candidates:
        if p.exists():
            return p
    return None


def _read_token_file() -> dict | None:
    """Read Deja's own token file, falling back to legacy gws token."""
    for path in [_DEJA_TOKEN_PATH, _GWS_TOKEN_PATH]:
        try:
            if path.exists():
                data = json.loads(path.read_text())
                if data:
                    return data
        except (json.JSONDecodeError, OSError) as e:
            log.debug("Failed to read %s: %s", path, e)
    return None


def _save_token(token_data: dict) -> None:
    """Save token to Deja's own token file."""
    _DEJA_TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    _DEJA_TOKEN_PATH.write_text(json.dumps(token_data, indent=2))


def _is_expired(token_data: dict) -> bool:
    """Check whether the access token has expired."""
    expiry = token_data.get("expiry") or token_data.get("token_expiry")
    if not expiry:
        return True
    try:
        if isinstance(expiry, (int, float)):
            return time.time() >= expiry
        dt = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
        return datetime.now(timezone.utc) >= dt
    except Exception:
        return True


def _refresh_token_native(token_data: dict) -> bool:
    """Refresh the OAuth token using google-auth."""
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials

        creds = Credentials(
            token=token_data.get("access_token") or token_data.get("token"),
            refresh_token=token_data.get("refresh_token"),
            token_uri="https://oauth2.googleapis.com/token",
            client_id=token_data.get("client_id"),
            client_secret=token_data.get("client_secret"),
            scopes=token_data.get("scopes"),
        )
        creds.refresh(Request())

        token_data["access_token"] = creds.token
        token_data["token"] = creds.token
        if creds.expiry:
            token_data["expiry"] = creds.expiry.isoformat()
        if creds.id_token:
            token_data["id_token"] = creds.id_token
        _save_token(token_data)
        return True
    except Exception as e:
        log.warning("Native token refresh failed: %s", e)
        return False


def _refresh_token_gws() -> bool:
    """Legacy: ask gws CLI to refresh. Returns True on success."""
    try:
        r = subprocess.run(
            ["gws", "auth", "refresh"],
            capture_output=True, text=True, timeout=15,
        )
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def get_auth_token() -> str | None:
    """Return a valid Google OAuth token string, or None if not authenticated.

    Reads Deja's own token file (~/.deja/google_token.json), falling back
    to the legacy gws token. Refreshes automatically if expired.
    Returns the id_token if present, otherwise the access_token.
    """
    token_data = _read_token_file()
    if not token_data:
        return None

    if _is_expired(token_data):
        # Try native refresh first, fall back to gws CLI
        if not _refresh_token_native(token_data):
            if not _refresh_token_gws():
                log.warning("OAuth token expired and all refresh methods failed")
                return None
            # Re-read after gws refresh
            token_data = _read_token_file()
            if not token_data:
                return None

    return (
        token_data.get("id_token")
        or token_data.get("access_token")
        or token_data.get("token")
    )


def get_user_email() -> str:
    """Extract the user's email from the token file."""
    token_data = _read_token_file()
    if token_data:
        email = token_data.get("user") or token_data.get("email")
        if email:
            return email

    # Fall back to gws auth status
    try:
        r = subprocess.run(
            ["gws", "auth", "status"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            status = json.loads(r.stdout)
            return status.get("user", "")
    except Exception:
        pass

    return ""


def run_oauth_flow(port: int = 0) -> dict:
    """Run the native OAuth flow. Opens browser for Google sign-in.

    Returns a dict with {ok, email, name, error} on completion.
    The token is saved to ~/.deja/google_token.json automatically.
    """
    client_secret = _find_client_secret()
    if not client_secret:
        return {"ok": False, "error": "client_secret.json not found"}

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow

        flow = InstalledAppFlow.from_client_secrets_file(
            str(client_secret), scopes=SCOPES
        )
        creds = flow.run_local_server(
            port=port,
            prompt="consent",
            success_message="Signed in to Déjà! You can close this tab.",
        )

        # Build token data
        client_config = json.loads(client_secret.read_text())
        client_type = list(client_config.keys())[0]
        client_info = client_config[client_type]

        token_data = {
            "access_token": creds.token,
            "token": creds.token,
            "refresh_token": creds.refresh_token,
            "id_token": getattr(creds, "id_token", None),
            "expiry": creds.expiry.isoformat() if creds.expiry else None,
            "scopes": list(creds.scopes) if creds.scopes else SCOPES,
            "client_id": client_info.get("client_id"),
            "client_secret": client_info.get("client_secret"),
        }

        # Get user info
        email = ""
        name = ""
        try:
            from google.auth.transport.requests import Request
            import httpx

            resp = httpx.get(
                "https://www.googleapis.com/oauth2/v2/userinfo",
                headers={"Authorization": f"Bearer {creds.token}"},
            )
            if resp.status_code == 200:
                info = resp.json()
                email = info.get("email", "")
                name = info.get("name", "")
        except Exception as e:
            log.debug("Failed to fetch user info: %s", e)

        token_data["email"] = email
        token_data["user"] = email
        _save_token(token_data)

        return {"ok": True, "email": email, "name": name}

    except Exception as e:
        log.exception("OAuth flow failed")
        return {"ok": False, "error": str(e)}
