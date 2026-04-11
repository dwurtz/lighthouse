"""MCP client detection + toggle endpoints.

GET  /api/mcp/clients              — list detected MCP-compatible clients
POST /api/mcp/clients/toggle       — enable/disable Deja on one client

The Swift settings view calls these to render the "Connected AI
Assistants" section and to flip Deja's server entry on each client.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

from deja.mcp_install import list_clients, set_enabled

log = logging.getLogger("deja.web.mcp")

router = APIRouter()


@router.get("/api/mcp/clients")
def get_mcp_clients() -> dict:
    """Return the list of known MCP clients with detection + enabled state."""
    return {"clients": list_clients()}


@router.post("/api/mcp/clients/toggle")
def toggle_mcp_client(payload: dict = Body(...)) -> JSONResponse:
    """Enable or disable Deja's MCP server for one named client.

    Body: ``{"client_name": "Claude Desktop", "enabled": true}``

    Returns the ``InstallResult`` as JSON. 400 on bad input or unknown
    client; 500 on filesystem errors.
    """
    client_name = payload.get("client_name")
    enabled = payload.get("enabled")

    if not isinstance(client_name, str) or not client_name:
        return JSONResponse(
            {"error": "client_name (string) is required"}, status_code=400
        )
    if not isinstance(enabled, bool):
        return JSONResponse(
            {"error": "enabled (bool) is required"}, status_code=400
        )

    try:
        result = set_enabled(client_name, enabled)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except OSError as e:
        log.warning("set_enabled(%s, %s) failed: %s", client_name, enabled, e)
        return JSONResponse({"error": str(e)}, status_code=500)

    return JSONResponse({
        "client_name": result.client_name,
        "installed": result.installed,
        "configured": result.configured,
        "already_ok": result.already_ok,
        "skipped": result.skipped,
        "config_path": result.config_path,
    })
