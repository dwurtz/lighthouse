"""Core FastAPI app creation, middleware, and route inclusion.

This is the single source of truth for the ``app`` instance. Route
modules define ``APIRouter`` objects that are included here.
"""

from __future__ import annotations

from fastapi import FastAPI

from deja.web.setup_routes import router as setup_router
from deja.web.chat_routes import router as chat_router
from deja.web.contact_routes import router as contact_router
from deja.web.meeting_routes import router as meeting_router
from deja.web.mic_routes import router as mic_router
from deja.web.status_routes import router as status_router


def create_app() -> FastAPI:
    """Build and return the fully-configured FastAPI application.

    Authentication is handled by Unix domain socket filesystem
    permissions (``~/.deja/`` is chmod 700), so no shared-secret
    middleware is needed.
    """
    application = FastAPI(title="deja", version="0.2.0")

    # Include routers
    application.include_router(setup_router)
    application.include_router(status_router)
    application.include_router(contact_router)
    application.include_router(chat_router)
    application.include_router(mic_router)
    application.include_router(meeting_router)

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
