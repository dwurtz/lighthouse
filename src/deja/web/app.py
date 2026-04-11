"""Core FastAPI app creation, middleware, and route inclusion.

This is the single source of truth for the ``app`` instance. Route
modules define ``APIRouter`` objects that are included here.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from deja.mcp_install import _repair_stale_paths, _run_mcp_first_install
from deja.web.setup_routes import router as setup_router
from deja.web.command_routes import router as command_router
from deja.web.contact_routes import router as contact_router
from deja.web.mcp_routes import router as mcp_router
from deja.web.meeting_routes import router as meeting_router
from deja.web.mic_routes import router as mic_router
from deja.web.status_routes import router as status_router

log = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI):  # noqa: ARG001 — FastAPI requires this signature
    """App lifespan — runs MCP auto-install + stale-path repair on startup.

    Fire-and-forget: we dispatch both helpers in a background thread
    and return immediately so health checks and the rest of startup
    are never blocked on config-file I/O. If one client's filesystem
    is slow (network home dir, locked file, etc.) the backend stays
    responsive. Both helpers are exception-safe — they log and swallow
    failures so a bad install can never crash the server.
    """
    def _run() -> None:
        try:
            _run_mcp_first_install()
            _repair_stale_paths()
        except Exception as e:  # noqa: BLE001 — belt-and-braces
            log.exception("MCP auto-install: unexpected failure: %s", e)

    try:
        asyncio.create_task(asyncio.to_thread(_run))
    except Exception as e:  # noqa: BLE001
        log.exception("MCP auto-install: failed to schedule task: %s", e)

    yield


def create_app() -> FastAPI:
    """Build and return the fully-configured FastAPI application.

    Authentication is handled by Unix domain socket filesystem
    permissions (``~/.deja/`` is chmod 700), so no shared-secret
    middleware is needed.
    """
    application = FastAPI(title="deja", version="0.2.0", lifespan=_lifespan)

    # Include routers
    application.include_router(setup_router)
    application.include_router(status_router)
    application.include_router(contact_router)
    application.include_router(command_router)
    application.include_router(mic_router)
    application.include_router(meeting_router)
    application.include_router(mcp_router)

    return application


app = create_app()


def run_web(port: int = 0) -> None:
    """Start the web server on a Unix domain socket.

    Called by ``python -m deja web``. Listens on
    ``~/.deja/deja.sock``.  Filesystem permissions on ``~/.deja/``
    (chmod 700) serve as the authentication boundary -- no shared
    secret is needed.

    The ``port`` parameter is accepted for backward compatibility
    but ignored; all IPC goes through the socket.
    """
    import atexit
    import os

    import uvicorn

    from deja.config import DEJA_HOME

    sock_path = str(DEJA_HOME / "deja.sock")

    # Clean up stale socket from a previous run
    if os.path.exists(sock_path):
        os.unlink(sock_path)

    # Register cleanup so socket is removed on exit
    def _cleanup():
        try:
            os.unlink(sock_path)
        except FileNotFoundError:
            pass

    atexit.register(_cleanup)

    print(f"Déjà: unix://{sock_path}")
    uvicorn.run(app, uds=sock_path, log_level="info")
