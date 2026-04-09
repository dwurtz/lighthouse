"""First-launch setup API endpoints.

Called by the Swift setup wizard to configure Deja without
any terminal usage. The wizard walks through:
  1. Google Workspace OAuth (browser-based)
  2. User identity (name, email → self-page)
  3. Wiki initialization (dirs, prompts, git)

All endpoints are idempotent — safe to call multiple times.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

from fastapi import APIRouter

from deja.config import DEJA_HOME, WIKI_DIR

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/setup")


@router.get("/status")
def setup_status() -> dict:
    """Check what's already configured."""
    import os
    import httpx as _httpx
    from deja.llm_client import DEJA_API_URL

    # Check server reachability (or dev direct mode)
    server_reachable = False
    if os.environ.get("GEMINI_API_KEY"):
        server_reachable = True  # dev direct mode
    else:
        try:
            r = _httpx.get(f"{DEJA_API_URL}/v1/health", timeout=5)
            server_reachable = r.status_code < 500
        except Exception:
            pass

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
        from deja.identity import load_user
        user = load_user()
        has_identity = not user.is_generic
        user_name = user.name or ""
    except Exception:
        pass

    # Check wiki
    wiki_exists = (WIKI_DIR / "index.md").exists()

    # Check setup_done
    setup_done = (DEJA_HOME / "setup_done").exists()

    return {
        "setup_done": setup_done,
        "server_reachable": server_reachable,
        "gws_authenticated": gws_authed,
        "gws_email": gws_email,
        "has_identity": has_identity,
        "user_name": user_name,
        "wiki_exists": wiki_exists,
    }


@router.post("/gws-auth")
def start_gws_auth() -> dict:
    """Start Google Workspace OAuth flow.

    Opens the browser for Google sign-in. On success, extracts the
    user's email and name, creates the identity self-page, and
    initializes the wiki — combining what used to be separate
    "Google auth" and "identity" steps into one.

    The OAuth client_secret.json is bundled inside the app. On first
    run, it's copied to ~/.config/gws/ so gws CLI can find it.
    """
    # Ensure client_secret.json exists for gws CLI
    import shutil
    import sys
    gws_config = Path.home() / ".config" / "gws"
    client_secret = gws_config / "client_secret.json"
    if not client_secret.exists():
        gws_config.mkdir(parents=True, exist_ok=True)
        # Try bundled client_secret from multiple locations:
        # 1. Package default_assets (development / pip install -e)
        # 2. App bundle Resources (macOS .app distribution)
        candidates = [
            Path(__file__).parent / "default_assets" / "client_secret.json",
        ]
        # When running inside Deja.app, the bundle Resources dir is two levels
        # up from Contents/MacOS/deja-backend: .../Contents/Resources/
        if getattr(sys, 'frozen', False):
            bundle_resources = Path(sys.executable).parent.parent / "Resources" / "client_secret.json"
            candidates.append(bundle_resources)
        else:
            # Development: check relative to project root
            candidates.append(Path(__file__).parents[3] / "Deja.app" / "Contents" / "Resources" / "client_secret.json")
        for bundled in candidates:
            if bundled.exists():
                shutil.copy(bundled, client_secret)
                log.info("Copied client_secret.json from %s to ~/.config/gws/", bundled)
                break

    already_authed = False
    try:
        r = subprocess.run(
            ["gws", "auth", "status"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            import json as _json
            try:
                status = _json.loads(r.stdout)
                already_authed = status.get("token_valid", False)
            except _json.JSONDecodeError:
                pass
    except FileNotFoundError:
        return {"ok": False, "error": "gws CLI not found"}
    except Exception:
        pass

    if not already_authed:
        # Start OAuth flow — capture the auth URL and open the browser
        # explicitly, since the frozen backend runs headless.
        try:
            proc = subprocess.Popen(
                ["gws", "auth", "login"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,  # merge stderr into stdout
                text=True,
            )
            # gws prints the auth URL to stderr. We merged it into
            # stdout so we only need to read one stream.
            auth_url = None
            import time
            start = time.time()
            while time.time() - start < 15:
                line = proc.stdout.readline()
                if not line:
                    if proc.poll() is not None:
                        break  # process exited
                    continue
                log.info("gws auth output: %s", line.rstrip())
                if "accounts.google.com" in line:
                    auth_url = line.strip()
                    break
            if auth_url:
                # Rewrite the redirect_uri to point to our own callback server
                # so we can show a branded success page instead of the plain gws one.
                # Extract the port gws is listening on from the URL.
                import urllib.parse
                parsed = urllib.parse.urlparse(auth_url)
                params = urllib.parse.parse_qs(parsed.query)
                gws_redirect = params.get("redirect_uri", [""])[0]
                gws_port = gws_redirect.split(":")[-1] if gws_redirect else ""

                # Try branded callback page (optional — falls back to plain gws)
                try:
                    from deja._oauth_callback import start_branded_callback
                    our_port = start_branded_callback(gws_port=int(gws_port) if gws_port.isdigit() else 0)
                    if our_port:
                        new_redirect = f"http://localhost:{our_port}"
                        auth_url = auth_url.replace(gws_redirect, new_redirect)
                        log.info("Rewrote OAuth redirect to branded callback on port %d", our_port)
                except ImportError:
                    pass  # branded callback not available — use plain gws redirect

                subprocess.run(["open", auth_url])
                log.info("Opened Google auth URL in browser")
            # Wait for the callback (user signs in → gws receives the code)
            try:
                proc.wait(timeout=120)
            except subprocess.TimeoutExpired:
                proc.kill()
                return {"ok": False, "error": "Auth timed out — try again"}
            if proc.returncode != 0:
                return {"ok": False, "error": "Auth failed"}
        except FileNotFoundError:
            return {"ok": False, "error": "gws CLI not found"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # Extract user email from gws auth status
    email = ""
    try:
        r = subprocess.run(
            ["gws", "auth", "status"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            import json as _json
            try:
                status = _json.loads(r.stdout)
                email = status.get("user", "")
            except _json.JSONDecodeError:
                pass
    except Exception:
        pass

    # Try to get display name from Gmail profile
    name = ""
    if email:
        # Derive name from email: david@example.com → David
        local_part = email.split("@")[0]
        # Handle first.last format
        parts = local_part.replace(".", " ").replace("-", " ").replace("_", " ").split()
        name = " ".join(p.capitalize() for p in parts)

    # Also try macOS Contacts for a better name
    if email:
        try:
            r = subprocess.run(
                ["gws", "gmail", "users", "getProfile",
                 "--params", json.dumps({"userId": "me"})],
                capture_output=True, text=True, timeout=10,
            )
        except Exception:
            pass

    # Auto-create identity if we have email
    if email:
        result = set_identity({
            "name": name,
            "email": email,
        })
        return {
            "ok": True,
            "email": email,
            "name": name,
            "identity_created": result.get("ok", False),
            "already": already_authed,
        }

    return {"ok": True, "email": "", "name": "", "already": already_authed}


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
            f"The user behind Deja.\n"
        )

    # Copy default prompts if not present
    try:
        import importlib.resources as pkg_resources
        prompts_dir = WIKI_DIR / "prompts"
        for prompt_name in ["integrate", "reflect", "describe_screen", "prefilter", "chat", "onboard"]:
            dest = prompts_dir / f"{prompt_name}.md"
            if not dest.exists():
                try:
                    src = pkg_resources.files("deja") / "default_assets" / "prompts" / f"{prompt_name}.md"
                    if src.is_file():
                        dest.write_text(src.read_text())
                        log.info("Copied default prompt: %s", prompt_name)
                    else:
                        log.warning("Default prompt not found in package: %s", prompt_name)
                except Exception:
                    log.warning("Failed to copy default prompt: %s", prompt_name, exc_info=True)
    except Exception:
        log.error("Default prompts copy failed entirely", exc_info=True)

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
        from deja.wiki_git import ensure_repo
        ensure_repo()
    except Exception:
        log.debug("git init failed", exc_info=True)

    # Rebuild index
    try:
        from deja.wiki_catalog import rebuild_index
        rebuild_index()
    except Exception:
        pass

    return {"ok": True, "slug": slug}


@router.post("/complete")
def complete_setup() -> dict:
    """Mark setup as complete. Writes the setup_done marker."""
    DEJA_HOME.mkdir(parents=True, exist_ok=True)
    (DEJA_HOME / "setup_done").write_text("")

    # Run MCP auto-install silently
    try:
        from deja.mcp_install import install_mcp_servers
        install_mcp_servers()
    except Exception:
        log.debug("MCP auto-install failed", exc_info=True)

    return {"ok": True}


# ---------------------------------------------------------------------------
# Backfill progress
# ---------------------------------------------------------------------------

_backfill_progress: dict = {
    "running": False,
    "current_step": "",
    "step_index": 0,
    "total_steps": 0,
    "batch": 0,
    "total_batches": 0,
    "pages_written": 0,
    "completed_steps": [],
}


@router.get("/backfill-status")
def backfill_status() -> dict:
    """Return current backfill progress."""
    return _backfill_progress


@router.post("/start-backfill")
async def start_backfill() -> dict:
    """Start the 30-day backfill in the background.

    Returns immediately. Poll /api/setup/backfill-status for progress.
    """
    import asyncio

    if _backfill_progress["running"]:
        return {"ok": True, "already_running": True}

    asyncio.create_task(_run_backfill())
    return {"ok": True}


async def _run_backfill() -> None:
    """Run all onboarding steps with progress tracking."""
    from deja.onboarding import ALL_STEPS, is_step_done
    from deja.onboarding.runner import run_step
    from deja.llm_client import GeminiClient
    import asyncio

    pending = [(name, desc) for name, desc in ALL_STEPS if not is_step_done(name)]
    if not pending:
        _backfill_progress["running"] = False
        _backfill_progress["current_step"] = "done"
        return

    _backfill_progress["running"] = True
    _backfill_progress["total_steps"] = len(pending)
    _backfill_progress["completed_steps"] = []

    gemini = GeminiClient()
    wiki_lock = asyncio.Lock()

    for i, (name, desc) in enumerate(pending):
        _backfill_progress["step_index"] = i + 1
        _backfill_progress["current_step"] = desc
        _backfill_progress["batch"] = 0
        _backfill_progress["total_batches"] = 0

        def on_progress(info: dict) -> None:
            _backfill_progress["batch"] = info.get("batch", 0)
            _backfill_progress["total_batches"] = info.get("total_batches", 0)
            _backfill_progress["pages_written"] = info.get("pages_written", 0)

        # Import the fetch function for each step
        fetch_fn = _get_fetch_fn(name)
        if fetch_fn is None:
            continue

        try:
            summary = await run_step(
                name=name,
                fetch_fn=fetch_fn,
                wiki_lock=wiki_lock,
                gemini=gemini,
                on_progress=on_progress,
            )
            _backfill_progress["completed_steps"].append({
                "name": name,
                "desc": desc,
                "pages": summary.get("pages_written", 0),
            })
        except Exception:
            log.exception("Backfill step %s failed", name)

    _backfill_progress["running"] = False
    _backfill_progress["current_step"] = "done"

    # Commit wiki changes
    try:
        from deja.wiki_git import commit_changes
        from deja.wiki_catalog import rebuild_index
        rebuild_index()
        commit_changes("onboarding: 30-day backfill complete")
    except Exception:
        pass


def _get_fetch_fn(step_name: str):
    """Return the fetch function for a given onboarding step."""
    if step_name == "sent_email_backfill":
        from deja.observations.email import fetch_sent_threads_backfill
        return lambda: fetch_sent_threads_backfill(days=30)
    elif step_name == "imessage_backfill":
        from deja.observations.imessage import fetch_imessage_contacts_backfill
        return lambda: fetch_imessage_contacts_backfill(days=30)
    elif step_name == "whatsapp_backfill":
        from deja.observations.whatsapp import fetch_whatsapp_contacts_backfill
        return lambda: fetch_whatsapp_contacts_backfill(days=30)
    elif step_name == "calendar_backfill":
        from deja.observations.calendar import fetch_calendar_backfill
        return lambda: fetch_calendar_backfill(days=30)
    return None
