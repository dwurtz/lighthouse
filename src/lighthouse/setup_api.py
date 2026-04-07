"""First-launch setup API endpoints.

Called by the Swift setup wizard to configure Lighthouse without
any terminal usage. The wizard walks through:
  1. Gemini API key (validate + store in Keychain)
  2. Google Workspace OAuth (browser-based)
  3. User identity (name, email → self-page)
  4. Wiki initialization (dirs, prompts, git)

All endpoints are idempotent — safe to call multiple times.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

from fastapi import APIRouter

from lighthouse.config import LIGHTHOUSE_HOME, WIKI_DIR

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/setup")


@router.get("/status")
def setup_status() -> dict:
    """Check what's already configured."""
    from lighthouse.secrets import get_api_key, api_key_source

    key = get_api_key()
    has_key = bool(key)

    # Check gws auth — output is JSON with token_valid and user fields
    gws_authed = False
    gws_email = ""
    try:
        r = subprocess.run(
            ["gws", "auth", "status"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            import json as _json
            try:
                status = _json.loads(r.stdout)
                gws_authed = status.get("token_valid", False) and status.get("has_refresh_token", False)
                gws_email = status.get("user", "")
            except _json.JSONDecodeError:
                # Fallback: check for keywords
                if "token_valid" in r.stdout and "true" in r.stdout.lower():
                    gws_authed = True
    except Exception:
        pass

    # Check identity
    has_identity = False
    user_name = ""
    try:
        from lighthouse.identity import load_user
        user = load_user()
        has_identity = not user.is_generic
        user_name = user.name or ""
    except Exception:
        pass

    # Check wiki
    wiki_exists = (WIKI_DIR / "index.md").exists()

    # Check setup_done
    setup_done = (LIGHTHOUSE_HOME / "setup_done").exists()

    return {
        "setup_done": setup_done,
        "has_api_key": has_key,
        "api_key_source": api_key_source() if has_key else None,
        "gws_authenticated": gws_authed,
        "gws_email": gws_email,
        "has_identity": has_identity,
        "user_name": user_name,
        "wiki_exists": wiki_exists,
    }


@router.post("/api-key")
def set_api_key(body: dict) -> dict:
    """Validate and store the Gemini API key in macOS Keychain."""
    key = (body.get("key") or "").strip()
    if not key:
        return {"ok": False, "error": "No key provided"}

    # Validate by making a trivial API call
    try:
        from google import genai
        client = genai.Client(api_key=key)
        # Simple model list call to validate the key
        models = client.models.list()
        # If we get here, the key works
    except Exception as e:
        err = str(e)
        if "API_KEY_INVALID" in err or "401" in err or "403" in err:
            return {"ok": False, "error": "Invalid API key. Check it and try again."}
        # Other errors might be transient — store anyway
        log.warning("API key validation inconclusive: %s", err)

    # Store in Keychain
    try:
        from lighthouse.secrets import store_api_key
        store_api_key(key)
    except Exception as e:
        return {"ok": False, "error": f"Failed to store key: {e}"}

    # Also set in current process environment
    import os
    os.environ["GEMINI_API_KEY"] = key

    return {"ok": True}


@router.post("/gws-auth")
def start_gws_auth() -> dict:
    """Start Google Workspace OAuth flow.

    Opens the browser for Google sign-in. Returns when auth completes
    or after timeout.
    """
    try:
        # Check if already authenticated
        r = subprocess.run(
            ["gws", "auth", "status"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0 and "authenticated" in r.stdout.lower():
            return {"ok": True, "already": True}
    except FileNotFoundError:
        return {"ok": False, "error": "gws CLI not found. Install with: npm install -g @anthropic/gws-cli"}
    except Exception:
        pass

    # Start OAuth flow (opens browser)
    try:
        r = subprocess.run(
            ["gws", "auth", "login"],
            capture_output=True, text=True, timeout=120,
        )
        if r.returncode == 0:
            return {"ok": True}
        return {"ok": False, "error": r.stderr[:200] or "Auth failed"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Auth timed out — try again"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.post("/identity")
def set_identity(body: dict) -> dict:
    """Create the user's self-page and initialize the wiki."""
    name = (body.get("name") or "").strip()
    email = (body.get("email") or "").strip()
    preferred_name = (body.get("preferred_name") or name.split()[0] if name else "").strip()

    if not name or not email:
        return {"ok": False, "error": "Name and email are required"}

    import re
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")

    # Initialize wiki directory structure
    WIKI_DIR.mkdir(parents=True, exist_ok=True)
    for subdir in ["people", "projects", "events", "prompts"]:
        (WIKI_DIR / subdir).mkdir(exist_ok=True)

    # Create self-page
    self_page = WIKI_DIR / "people" / f"{slug}.md"
    if not self_page.exists():
        self_page.write_text(
            f"---\nself: true\nemail: {email}\n"
            f"preferred_name: {preferred_name}\n---\n\n"
            f"# {name}\n\n"
            f"The user behind Lighthouse.\n"
        )

    # Copy default prompts if not present
    try:
        import importlib.resources as pkg_resources
        prompts_dir = WIKI_DIR / "prompts"
        for prompt_name in ["integrate", "reflect", "describe_screen", "prefilter", "chat", "onboard"]:
            dest = prompts_dir / f"{prompt_name}.md"
            if not dest.exists():
                try:
                    src = pkg_resources.files("lighthouse") / "default_prompts" / f"{prompt_name}.md"
                    if src.is_file():
                        dest.write_text(src.read_text())
                except Exception:
                    pass
    except Exception:
        log.debug("Default prompts copy failed", exc_info=True)

    # Create goals.md if not present
    goals = WIKI_DIR / "goals.md"
    if not goals.exists():
        goals.write_text(
            "# Goals\n\n"
            "## Standing context\n\n\n"
            "## Automations\n\n\n"
            "## Tasks\n\n\n"
            "## Waiting for\n\n\n"
            "## Recurring\n\n"
        )

    # Create CLAUDE.md if not present
    claude_md = WIKI_DIR / "CLAUDE.md"
    if not claude_md.exists():
        claude_md.write_text(
            "# Wiki writing conventions\n\n"
            "Entity pages describe current state in clean prose.\n"
            "Event pages describe what happened with timestamps and [[wiki-links]].\n"
        )

    # Initialize git repo
    try:
        from lighthouse.wiki_git import ensure_repo
        ensure_repo()
    except Exception:
        log.debug("git init failed", exc_info=True)

    # Rebuild index
    try:
        from lighthouse.wiki_catalog import rebuild_index
        rebuild_index()
    except Exception:
        pass

    return {"ok": True, "slug": slug}


@router.post("/complete")
def complete_setup() -> dict:
    """Mark setup as complete. Writes the setup_done marker."""
    LIGHTHOUSE_HOME.mkdir(parents=True, exist_ok=True)
    (LIGHTHOUSE_HOME / "setup_done").write_text("")

    # Run MCP auto-install silently
    try:
        from lighthouse.mcp_install import install_mcp_servers
        install_mcp_servers()
    except Exception:
        log.debug("MCP auto-install failed", exc_info=True)

    return {"ok": True}
