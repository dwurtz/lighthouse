"""Core FastAPI app creation, middleware, and route inclusion.

This is the single source of truth for the ``app`` instance. Route
modules define ``APIRouter`` objects that are included here.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from deja.web.setup_routes import router as setup_router
from deja.web.chat_routes import router as chat_router
from deja.web.contact_routes import router as contact_router
from deja.web.meeting_routes import router as meeting_router
from deja.web.mic_routes import router as mic_router
from deja.web.status_routes import router as status_router


def create_app() -> FastAPI:
    """Build and return the fully-configured FastAPI application."""
    import os
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse

    application = FastAPI(title="deja", version="0.2.0")

    # IPC authentication — the Swift app passes a random secret via
    # DEJA_IPC_SECRET env var. All requests must include it as
    # X-Deja-Secret header. This prevents other local processes from
    # accessing the backend.
    ipc_secret = os.environ.get("DEJA_IPC_SECRET")

    class IPCAuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            if ipc_secret:
                provided = request.headers.get("x-deja-secret", "")
                if provided != ipc_secret:
                    return JSONResponse(
                        {"error": "Unauthorized"},
                        status_code=401,
                    )
            return await call_next(request)

    application.add_middleware(IPCAuthMiddleware)

    # CORS — localhost only (Swift app + admin)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5055"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Include routers
    application.include_router(setup_router)
    application.include_router(status_router)
    application.include_router(contact_router)
    application.include_router(chat_router)
    application.include_router(mic_router)
    application.include_router(meeting_router)

    return application


app = create_app()


def run_web(port: int = 5055) -> None:
    """Start the web server. Called by ``python -m deja web``."""
    import uvicorn

    print(f"Déjà: http://localhost:{port}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
