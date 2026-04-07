"""Auto-configure Lighthouse as an MCP server on all detected AI clients.

Scans the system for installed MCP-compatible applications (Claude
Desktop, Cursor, Windsurf, VS Code) and writes the server configuration
into each one's config file. Safe to run multiple times — merges into
existing configs without overwriting other servers or preferences.

ChatGPT Desktop is detected but skipped because it only supports remote
(HTTP/SSE) MCP servers, not local stdio transport.

Called by ``lighthouse configure`` during first-run setup and can also
be run standalone for re-configuration after moving the venv.
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Client definitions
# ---------------------------------------------------------------------------

@dataclass
class MCPClient:
    """Describes one MCP-compatible application and how to configure it."""
    name: str
    app_path: Path          # .app bundle — used to detect if installed
    config_path: Path       # Where the MCP config lives
    root_key: str           # "mcpServers" or "servers"
    needs_type: bool        # VS Code requires "type": "stdio" per server
    create_dirs: bool       # Whether to mkdir -p the config's parent


def _home() -> Path:
    return Path.home()


CLIENTS: list[MCPClient] = [
    MCPClient(
        name="Claude Desktop",
        app_path=Path("/Applications/Claude.app"),
        config_path=_home() / "Library/Application Support/Claude/claude_desktop_config.json",
        root_key="mcpServers",
        needs_type=False,
        create_dirs=True,
    ),
    MCPClient(
        name="Cursor",
        app_path=Path("/Applications/Cursor.app"),
        config_path=_home() / ".cursor/mcp.json",
        root_key="mcpServers",
        needs_type=False,
        create_dirs=True,
    ),
    MCPClient(
        name="Windsurf",
        app_path=Path("/Applications/Windsurf.app"),
        config_path=_home() / ".codeium/windsurf/mcp_config.json",
        root_key="mcpServers",
        needs_type=False,
        create_dirs=True,
    ),
    MCPClient(
        name="VS Code",
        app_path=Path("/Applications/Visual Studio Code.app"),
        config_path=_home() / "Library/Application Support/Code/User/mcp.json",
        root_key="servers",
        needs_type=True,
        create_dirs=True,
    ),
]

# ChatGPT is detected for informational purposes but can't be auto-configured.
CHATGPT_APP = Path("/Applications/ChatGPT.app")


# ---------------------------------------------------------------------------
# Server entry builder
# ---------------------------------------------------------------------------

SERVER_NAME = "lighthouse"


def _python_path() -> str:
    """Return the absolute path to the current Python interpreter.

    This is the Python inside the Lighthouse venv — we need the
    absolute path because MCP clients launch the server as a child
    process with no shell environment, so ``python`` or ``python3``
    won't resolve to the right venv.

    Intentionally does NOT resolve symlinks — the venv's
    ``bin/python`` symlink is more stable than the underlying
    homebrew/system binary path (which changes on Python upgrades).
    """
    return str(Path(sys.executable))


def _server_entry(client: MCPClient) -> dict:
    """Build the server config dict for one client."""
    entry: dict = {
        "command": _python_path(),
        "args": ["-m", "lighthouse", "mcp"],
    }
    if client.needs_type:
        entry["type"] = "stdio"
    return entry


# ---------------------------------------------------------------------------
# Config file I/O — read, merge, write
# ---------------------------------------------------------------------------


def _read_config(path: Path) -> dict:
    """Read an existing JSON config file, or return {} if missing/corrupt."""
    if not path.exists():
        return {}
    try:
        text = path.read_text().strip()
        if not text:
            return {}
        return json.loads(text)
    except (json.JSONDecodeError, OSError) as e:
        log.warning("Could not parse %s: %s — will create fresh", path, e)
        return {}


def _write_config(path: Path, data: dict, create_dirs: bool) -> None:
    """Write a JSON config file, optionally creating parent dirs."""
    if create_dirs:
        path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def _merge_server(config: dict, root_key: str, server_name: str, entry: dict) -> bool:
    """Merge a server entry into the config dict under ``root_key``.

    Returns True if the config was changed (new server or updated entry),
    False if the existing entry was already identical.
    """
    servers = config.setdefault(root_key, {})
    existing = servers.get(server_name)
    if existing == entry:
        return False
    servers[server_name] = entry
    return True


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

@dataclass
class InstallResult:
    """Result of configuring one client."""
    client_name: str
    installed: bool     # App detected on disk
    configured: bool    # Config file was written/updated
    already_ok: bool    # Config was already correct (no write needed)
    skipped: str        # Reason for skipping (empty if configured)
    config_path: str    # Path that was written (or would have been)


def install_on_all(dry_run: bool = False) -> list[InstallResult]:
    """Detect installed MCP clients and configure Lighthouse on each.

    Returns a list of results — one per known client plus ChatGPT if
    detected. Safe to call repeatedly; only writes when the config
    actually needs to change.
    """
    results: list[InstallResult] = []

    for client in CLIENTS:
        if not client.app_path.exists():
            results.append(InstallResult(
                client_name=client.name,
                installed=False,
                configured=False,
                already_ok=False,
                skipped="not installed",
                config_path=str(client.config_path),
            ))
            continue

        entry = _server_entry(client)
        config = _read_config(client.config_path)
        changed = _merge_server(config, client.root_key, SERVER_NAME, entry)

        if not changed:
            results.append(InstallResult(
                client_name=client.name,
                installed=True,
                configured=True,
                already_ok=True,
                skipped="",
                config_path=str(client.config_path),
            ))
            continue

        if dry_run:
            results.append(InstallResult(
                client_name=client.name,
                installed=True,
                configured=False,
                already_ok=False,
                skipped="dry run",
                config_path=str(client.config_path),
            ))
            continue

        try:
            _write_config(client.config_path, config, client.create_dirs)
            results.append(InstallResult(
                client_name=client.name,
                installed=True,
                configured=True,
                already_ok=False,
                skipped="",
                config_path=str(client.config_path),
            ))
            log.info("Configured MCP server on %s at %s", client.name, client.config_path)
        except OSError as e:
            results.append(InstallResult(
                client_name=client.name,
                installed=True,
                configured=False,
                already_ok=False,
                skipped=f"write failed: {e}",
                config_path=str(client.config_path),
            ))
            log.warning("Failed to write %s config: %s", client.name, e)

    # ChatGPT — detect but can't auto-configure
    if CHATGPT_APP.exists():
        results.append(InstallResult(
            client_name="ChatGPT",
            installed=True,
            configured=False,
            already_ok=False,
            skipped="ChatGPT only supports remote MCP servers (HTTP/SSE), not local stdio. Configure manually via Settings > Developer Mode.",
            config_path="(GUI only)",
        ))

    return results


def print_install_report(results: list[InstallResult], indent: str = "") -> None:
    """Print a human-readable summary of install results."""
    for r in results:
        if not r.installed:
            print(f"{indent}{r.client_name}: not installed — skipped")
        elif r.already_ok:
            print(f"{indent}{r.client_name}: already configured")
        elif r.configured:
            print(f"{indent}{r.client_name}: configured at {r.config_path}")
        elif r.skipped:
            print(f"{indent}{r.client_name}: skipped — {r.skipped}")

    configured_count = sum(1 for r in results if r.configured)
    detected_count = sum(1 for r in results if r.installed)
    if configured_count:
        print(f"{indent}MCP server configured on {configured_count}/{detected_count} detected client(s)")
    elif detected_count:
        print(f"{indent}No clients needed configuration ({detected_count} detected, all up to date)")
    else:
        print(f"{indent}No MCP-compatible clients detected")
