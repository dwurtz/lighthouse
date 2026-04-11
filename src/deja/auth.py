"""Google OAuth for Deja API server authentication.

Uses google-auth-oauthlib for native OAuth — no external CLI dependency.
Tokens are stored in the macOS Keychain (service=deja, account=google-token)
for encryption at rest. Falls back to legacy file-based tokens for migration.
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
_KEYCHAIN_SERVICE = "deja"
_KEYCHAIN_ACCOUNT = "google-token"


def _keychain_read() -> str | None:
    """Read the token JSON blob from macOS Keychain."""
    try:
        r = subprocess.run(
            ["security", "find-generic-password",
             "-s", _KEYCHAIN_SERVICE, "-a", _KEYCHAIN_ACCOUNT, "-w"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def _keychain_write(value: str) -> bool:
    """Store the token JSON blob in macOS Keychain. Returns True on success."""
    try:
        subprocess.run(
            ["security", "add-generic-password",
             "-s", _KEYCHAIN_SERVICE, "-a", _KEYCHAIN_ACCOUNT,
             "-w", value, "-U"],
            check=True, capture_output=True, text=True, timeout=5,
        )
        return True
    except subprocess.CalledProcessError as e:
        # CAUTION: do not log `e` directly — its str() repr includes the
        # full argv, which contains the token JSON passed via -w.
        log.warning("Keychain write failed with exit code %s", e.returncode)
        return False
    except FileNotFoundError:
        log.warning("Keychain write failed: `security` command not found")
        return False
    except subprocess.TimeoutExpired:
        log.warning("Keychain write timed out after 5s")
        return False


def _keychain_delete() -> bool:
    """Remove the token from macOS Keychain."""
    try:
        r = subprocess.run(
            ["security", "delete-generic-password",
             "-s", _KEYCHAIN_SERVICE, "-a", _KEYCHAIN_ACCOUNT],
            capture_output=True, text=True,
        )
        return r.returncode == 0
    except Exception:
        return False

_BRANDED_CALLBACK_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Déjà — Signed In</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@400;500;600&family=Inter:wght@400;500&display=swap" rel="stylesheet">
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    background: #050507;
    color: #ece8e1;
    font-family: 'Inter', sans-serif;
    height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
    overflow: hidden;
  }
  .container {
    text-align: center;
    max-width: 440px;
    padding: 48px;
  }
  .logo {
    font-family: 'Cormorant Garamond', serif;
    font-size: 36px;
    font-weight: 500;
    letter-spacing: -0.5px;
    margin-bottom: 32px;
    color: #ece8e1;
  }
  .checkmark {
    width: 64px;
    height: 64px;
    margin: 0 auto 24px;
    border-radius: 50%;
    background: rgba(138, 180, 212, 0.1);
    border: 2px solid rgba(138, 180, 212, 0.3);
    display: flex;
    align-items: center;
    justify-content: center;
    animation: fadeIn 0.5s ease-out;
  }
  .checkmark svg {
    width: 28px;
    height: 28px;
    stroke: #8ab4d4;
    stroke-width: 2.5;
    fill: none;
    stroke-linecap: round;
    stroke-linejoin: round;
  }
  h1 {
    font-family: 'Cormorant Garamond', serif;
    font-size: 28px;
    font-weight: 500;
    margin-bottom: 12px;
    letter-spacing: -0.3px;
  }
  p {
    color: rgba(236, 232, 225, 0.5);
    font-size: 14px;
    line-height: 1.6;
  }
  .close-hint {
    margin-top: 32px;
    font-size: 12px;
    color: rgba(236, 232, 225, 0.25);
    font-family: 'DM Mono', monospace, monospace;
  }
  @keyframes fadeIn {
    from { opacity: 0; transform: scale(0.8); }
    to { opacity: 1; transform: scale(1); }
  }
</style>
</head>
<body>
<div class="container">
  <div class="logo">Déjà</div>
  <div class="checkmark">
    <svg viewBox="0 0 24 24"><polyline points="20 6 9 17 4 12"/></svg>
  </div>
  <h1>You're signed in</h1>
  <p>Your Google account is connected. You can return to the app.</p>
  <div class="close-hint">You can close this tab</div>
</div>
</body>
</html>"""
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


def _fetch_oauth_config() -> dict | None:
    """Fetch OAuth client config from the Deja API server.

    Returns {"client_id": ..., "client_secret": ..., "project_id": ...}
    or None if the server is unreachable.
    """
    try:
        import httpx
        import os

        api_url = os.environ.get("DEJA_API_URL", "https://deja-api.onrender.com")
        resp = httpx.get(f"{api_url}/v1/config", timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            client_id = data.get("oauth_client_id", "")
            client_secret = data.get("oauth_client_secret", "")
            if client_id and client_secret:
                return {
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "project_id": data.get("oauth_project_id", "deja-ai-app"),
                }
    except Exception as e:
        log.debug("Failed to fetch OAuth config from server: %s", e)
    return None


def _find_client_secret() -> Path | None:
    """Locate client_secret.json from bundled locations (fallback only).

    The primary path is _fetch_oauth_config() from the server.
    This is the fallback for dev mode or when the server is unreachable.
    """
    candidates = [
        Path.home() / ".config" / "gws" / "client_secret.json",
        Path(__file__).parent / "default_assets" / "client_secret.json",
    ]
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
    """Read token from Keychain, with file-based fallback for migration.

    Priority:
      1. macOS Keychain (encrypted at rest)
      2. ~/.deja/google_token.json (migrated to Keychain on first read)
      3. ~/.config/gws/token.json (legacy gws CLI)
    """
    # 1. Try Keychain
    raw = _keychain_read()
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass

    # 2. Try file-based tokens and migrate to Keychain
    for path in [_DEJA_TOKEN_PATH, _GWS_TOKEN_PATH]:
        try:
            if path.exists():
                data = json.loads(path.read_text())
                if data:
                    # Migrate to Keychain
                    if _keychain_write(json.dumps(data)):
                        log.info("Migrated token from %s to macOS Keychain", path)
                        # Remove the plain-text file after successful migration
                        if path == _DEJA_TOKEN_PATH:
                            try:
                                path.unlink()
                                log.info("Deleted plain-text token file: %s", path)
                            except OSError:
                                pass
                    return data
        except (json.JSONDecodeError, OSError) as e:
            log.debug("Failed to read %s: %s", path, e)
    return None


def _sync_gws_credentials(token_data: dict) -> None:
    """Write token in gws CLI format so observation collectors work.

    The gws CLI reads ~/.config/gws/credentials.json with a specific
    schema. This bridges our native OAuth token to gws format.
    """
    try:
        gws_dir = Path.home() / ".config" / "gws"
        gws_dir.mkdir(parents=True, exist_ok=True)
        creds = {
            "type": "authorized_user",
            "token": token_data.get("access_token") or token_data.get("token"),
            "refresh_token": token_data.get("refresh_token"),
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": token_data.get("client_id"),
            "client_secret": token_data.get("client_secret"),
            "scopes": token_data.get("scopes", []),
            "universe_domain": "googleapis.com",
            "account": "",
            "expiry": token_data.get("expiry"),
        }
        (gws_dir / "credentials.json").write_text(json.dumps(creds, indent=2))
    except Exception as e:
        log.debug("Failed to sync gws credentials: %s", e)


def _save_token(token_data: dict) -> None:
    """Save token to macOS Keychain (encrypted at rest).

    Falls back to file if Keychain write fails.
    Also syncs to gws CLI format for observation collectors.
    """
    # Always sync to gws format so collectors work
    _sync_gws_credentials(token_data)

    token_json = json.dumps(token_data)
    if _keychain_write(token_json):
        # Clean up plain-text file if it exists
        if _DEJA_TOKEN_PATH.exists():
            try:
                _DEJA_TOKEN_PATH.unlink()
            except OSError:
                pass
        return

    # Fallback to file (shouldn't happen on macOS, but just in case)
    log.warning("Keychain write failed — falling back to file storage")
    _DEJA_TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    _DEJA_TOKEN_PATH.write_text(token_json)
    try:
        import os
        os.chmod(str(_DEJA_TOKEN_PATH), 0o600)
    except OSError:
        pass


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
        # CAUTION: do not log `e` in full — google-auth RefreshError can
        # include the raw HTTP response body from oauth2.googleapis.com,
        # which in some failure modes echoes the refresh_token / client_secret
        # back in the error payload. Log the exception type only.
        log.warning("Native token refresh failed: %s", type(e).__name__)
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


def get_access_token() -> str | None:
    """Return a valid Google OAuth access_token for calling Google APIs.

    Unlike get_auth_token() which prefers id_token (for server auth),
    this always returns the access_token (needed for Calendar, Gmail, etc.).
    """
    token_data = _read_token_file()
    if not token_data:
        return None

    if _is_expired(token_data):
        if not _refresh_token_native(token_data):
            if not _refresh_token_gws():
                log.warning("OAuth token expired and all refresh methods failed")
                return None
            token_data = _read_token_file()
            if not token_data:
                return None

    return token_data.get("access_token") or token_data.get("token")


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
    # Try fetching OAuth config from server first (no bundled secret needed)
    oauth_config = _fetch_oauth_config()
    client_secret_file = None
    if not oauth_config:
        client_secret_file = _find_client_secret()
        if not client_secret_file:
            return {"ok": False, "error": "OAuth configuration unavailable"}

    try:
        import os
        os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"  # allow localhost HTTP

        from google_auth_oauthlib.flow import InstalledAppFlow
        import webbrowser
        import wsgiref.simple_server

        if oauth_config:
            # Build flow from server-provided config (no file on disk)
            client_config = {
                "installed": {
                    "client_id": oauth_config["client_id"],
                    "client_secret": oauth_config["client_secret"],
                    "project_id": oauth_config["project_id"],
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
                    "redirect_uris": ["http://localhost"],
                }
            }
            flow = InstalledAppFlow.from_client_config(client_config, scopes=SCOPES)
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(client_secret_file), scopes=SCOPES
            )

        # Custom local server with branded callback page
        flow.redirect_uri = f"http://localhost:{port or 0}/"
        auth_url, _ = flow.authorization_url(
            access_type="offline",
            prompt="consent",
        )

        # Collect the authorization response
        auth_response_url = [None]

        class _CallbackHandler(wsgiref.simple_server.WSGIRequestHandler):
            def log_message(self, format, *args):
                pass  # suppress request logging

        def _wsgi_app(environ, start_response):
            from urllib.parse import urlparse, parse_qs
            query = parse_qs(environ.get("QUERY_STRING", ""))
            if "code" in query:
                auth_response_url[0] = (
                    f"http://localhost:{server.server_port}"
                    f"{environ.get('PATH_INFO', '/')}?{environ.get('QUERY_STRING', '')}"
                )
            start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")])
            return [_BRANDED_CALLBACK_HTML.encode()]

        wsgiref.simple_server.WSGIServer.allow_reuse_address = True
        server = wsgiref.simple_server.make_server(
            "localhost", port or 0, _wsgi_app, handler_class=_CallbackHandler
        )
        flow.redirect_uri = f"http://localhost:{server.server_port}/"
        auth_url, _ = flow.authorization_url(
            access_type="offline",
            prompt="consent",
        )

        webbrowser.open(auth_url)
        server.handle_request()
        server.server_close()

        if not auth_response_url[0]:
            return {"ok": False, "error": "No authorization response received"}

        flow.fetch_token(authorization_response=auth_response_url[0])
        creds = flow.credentials

        # Build token data — get client_id/secret from whichever source we used
        if oauth_config:
            stored_client_id = oauth_config["client_id"]
            stored_client_secret = oauth_config["client_secret"]
        else:
            _cfg = json.loads(client_secret_file.read_text())
            _type = list(_cfg.keys())[0]
            stored_client_id = _cfg[_type].get("client_id", "")
            stored_client_secret = _cfg[_type].get("client_secret", "")

        token_data = {
            "access_token": creds.token,
            "token": creds.token,
            "refresh_token": creds.refresh_token,
            "id_token": getattr(creds, "id_token", None),
            "expiry": creds.expiry.isoformat() if creds.expiry else None,
            "scopes": list(creds.scopes) if creds.scopes else SCOPES,
            "client_id": stored_client_id,
            "client_secret": stored_client_secret,
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
