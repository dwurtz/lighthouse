"""Auto-configure Deja as an MCP server on all detected AI clients.

Scans the system for installed MCP-compatible applications (Claude
Desktop, Claude Code, Cursor, Windsurf, VS Code) and writes the server
configuration into each one's config file. Safe to run multiple times —
merges into existing configs without overwriting other servers or
preferences.

ChatGPT Desktop is detected but skipped because it only supports remote
(HTTP/SSE) MCP servers, not local stdio transport.

Called by ``deja configure`` during first-run setup and can also
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
    detect_path: Path       # .app bundle or CLI config dir — presence means "installed"
    config_path: Path       # Where the MCP config lives
    root_key: str           # "mcpServers" or "servers"
    needs_type: bool        # VS Code requires "type": "stdio" per server
    create_dirs: bool       # Whether to mkdir -p the config's parent


def _home() -> Path:
    return Path.home()


CLIENTS: list[MCPClient] = [
    MCPClient(
        name="Claude Desktop",
        detect_path=Path("/Applications/Claude.app"),
        config_path=_home() / "Library/Application Support/Claude/claude_desktop_config.json",
        root_key="mcpServers",
        needs_type=False,
        create_dirs=True,
    ),
    MCPClient(
        name="Claude Code",
        # Claude Code is a CLI — no .app bundle. Detect by the presence of its
        # config dir (~/.claude/) which is created on first run of the CLI.
        detect_path=_home() / ".claude",
        config_path=_home() / ".claude/mcp.json",
        root_key="mcpServers",
        needs_type=False,
        create_dirs=True,
    ),
    MCPClient(
        name="Cursor",
        detect_path=Path("/Applications/Cursor.app"),
        config_path=_home() / ".cursor/mcp.json",
        root_key="mcpServers",
        needs_type=False,
        create_dirs=True,
    ),
    MCPClient(
        name="Windsurf",
        detect_path=Path("/Applications/Windsurf.app"),
        config_path=_home() / ".codeium/windsurf/mcp_config.json",
        root_key="mcpServers",
        needs_type=False,
        create_dirs=True,
    ),
    MCPClient(
        name="VS Code",
        detect_path=Path("/Applications/Visual Studio Code.app"),
        config_path=_home() / "Library/Application Support/Code/User/mcp.json",
        root_key="servers",
        needs_type=True,
        create_dirs=True,
    ),
]

# ChatGPT is detected for informational purposes but can't be auto-configured.
CHATGPT_APP = Path("/Applications/ChatGPT.app")
CHATGPT_NOTE = (
    "Manual setup — ChatGPT only supports remote MCP servers (HTTP/SSE), "
    "not local stdio."
)


# ---------------------------------------------------------------------------
# Server entry builder
# ---------------------------------------------------------------------------

SERVER_NAME = "deja"


def _python_path() -> str:
    """Return the absolute path to the current Python interpreter.

    This is the Python inside the Deja venv — we need the
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
        "args": ["-m", "deja", "mcp"],
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
    """Detect installed MCP clients and configure Deja on each.

    Returns a list of results — one per known client plus ChatGPT if
    detected. Safe to call repeatedly; only writes when the config
    actually needs to change.
    """
    results: list[InstallResult] = []

    for client in CLIENTS:
        if not client.detect_path.exists():
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


def _find_client(client_name: str) -> MCPClient | None:
    for c in CLIENTS:
        if c.name == client_name:
            return c
    return None


def _is_enabled(client: MCPClient) -> bool:
    """True if Deja's server entry is present in the client's config."""
    config = _read_config(client.config_path)
    servers = config.get(client.root_key, {}) if isinstance(config, dict) else {}
    return isinstance(servers, dict) and SERVER_NAME in servers


def list_clients() -> list[dict]:
    """Return one info dict per known MCP client (including ChatGPT).

    Read-only — does not write any config files. Each dict has:

    - ``name``              display name
    - ``installed``         app/config dir detected on disk
    - ``enabled``           Deja server entry present in the client's config
    - ``config_path``       filesystem path for display
    - ``auto_configurable`` False for ChatGPT (manual setup only)
    - ``note``              human-readable reason / hint (may be empty)
    """
    clients: list[dict] = []
    for client in CLIENTS:
        installed = client.detect_path.exists()
        enabled = installed and _is_enabled(client)
        clients.append({
            "name": client.name,
            "installed": installed,
            "enabled": enabled,
            "config_path": str(client.config_path),
            "auto_configurable": True,
            "note": "" if installed else "Not installed",
        })

    # ChatGPT — info only, can't be auto-configured.
    chatgpt_installed = CHATGPT_APP.exists()
    clients.append({
        "name": "ChatGPT",
        "installed": chatgpt_installed,
        "enabled": False,
        "config_path": "(GUI only)",
        "auto_configurable": False,
        "note": CHATGPT_NOTE,
    })

    return clients


def set_enabled(client_name: str, enabled: bool) -> InstallResult:
    """Enable or disable Deja's MCP server for one named client.

    - ``enabled=True``  writes (or leaves in place) the server entry.
    - ``enabled=False`` removes the server entry if present.

    Raises ``ValueError`` if the client name is unknown or refers to a
    client that can't be auto-configured (ChatGPT). Returns an
    ``InstallResult`` describing the outcome; if the client isn't
    installed the result has ``skipped='not installed'``.
    """
    if client_name == "ChatGPT":
        raise ValueError("ChatGPT cannot be auto-configured (manual setup only).")

    client = _find_client(client_name)
    if client is None:
        raise ValueError(f"Unknown MCP client: {client_name!r}")

    if not client.detect_path.exists():
        return InstallResult(
            client_name=client.name,
            installed=False,
            configured=False,
            already_ok=False,
            skipped="not installed",
            config_path=str(client.config_path),
        )

    if enabled:
        # Reuse install logic: merge entry and write if changed.
        entry = _server_entry(client)
        config = _read_config(client.config_path)
        changed = _merge_server(config, client.root_key, SERVER_NAME, entry)
        if not changed:
            return InstallResult(
                client_name=client.name,
                installed=True,
                configured=True,
                already_ok=True,
                skipped="",
                config_path=str(client.config_path),
            )
        try:
            _write_config(client.config_path, config, client.create_dirs)
        except OSError as e:
            log.warning("Failed to write %s config: %s", client.name, e)
            raise
        log.info("Enabled MCP server on %s at %s", client.name, client.config_path)
        return InstallResult(
            client_name=client.name,
            installed=True,
            configured=True,
            already_ok=False,
            skipped="",
            config_path=str(client.config_path),
        )

    # enabled=False → remove the entry.
    config = _read_config(client.config_path)
    servers = config.get(client.root_key, {}) if isinstance(config, dict) else {}
    if not isinstance(servers, dict) or SERVER_NAME not in servers:
        return InstallResult(
            client_name=client.name,
            installed=True,
            configured=False,
            already_ok=True,
            skipped="",
            config_path=str(client.config_path),
        )
    servers.pop(SERVER_NAME, None)
    config[client.root_key] = servers
    try:
        _write_config(client.config_path, config, client.create_dirs)
    except OSError as e:
        log.warning("Failed to write %s config: %s", client.name, e)
        raise
    log.info("Disabled MCP server on %s at %s", client.name, client.config_path)
    return InstallResult(
        client_name=client.name,
        installed=True,
        configured=False,
        already_ok=False,
        skipped="",
        config_path=str(client.config_path),
    )


# ---------------------------------------------------------------------------
# Startup hooks — first-install gating + stale-path self-heal
# ---------------------------------------------------------------------------

_MARKER_FILENAME = ".mcp_installed"


def _marker_path() -> Path:
    """Location of the first-install marker file inside ``~/.deja/``."""
    from deja.config import DEJA_HOME
    return DEJA_HOME / _MARKER_FILENAME


def _run_mcp_first_install() -> None:
    """Run MCP auto-install once per machine, gated by a marker file.

    Called from the FastAPI lifespan handler on backend startup. On
    first launch (no marker), runs ``install_on_all()`` and drops a
    marker in ``~/.deja/.mcp_installed`` so subsequent launches are
    no-ops. The marker is written only on successful completion so a
    crashed install will be retried on the next launch. A zero-client
    detection still writes the marker -- "we tried and there was
    nothing to do" is a success, not a failure.
    """
    marker = _marker_path()
    if not marker.parent.exists():
        log.warning(
            "MCP auto-install: skipping — %s does not exist (backend state dir missing)",
            marker.parent,
        )
        return

    if marker.exists():
        log.debug("MCP auto-install: marker present at %s — skipping", marker)
        return

    log.info("MCP auto-install: first run — configuring detected clients")
    try:
        results = install_on_all()
    except Exception as e:  # noqa: BLE001 — never let this crash startup
        log.exception("MCP auto-install: install_on_all() failed: %s", e)
        return

    configured = sum(1 for r in results if r.configured and not r.already_ok)
    already_ok = sum(1 for r in results if r.already_ok)
    detected = sum(1 for r in results if r.installed)
    log.info(
        "MCP auto-install: %d newly configured, %d already ok, %d detected client(s)",
        configured,
        already_ok,
        detected,
    )

    try:
        marker.touch()
    except OSError as e:
        log.warning("MCP auto-install: could not write marker %s: %s", marker, e)


def _repair_stale_paths() -> None:
    """Rewrite any Deja MCP entries whose ``command`` path is stale.

    Runs on every startup (no marker gating). For each known client,
    if the client has a ``deja`` entry AND the entry's ``command``
    field doesn't match ``sys.executable``, rewrite it with the
    current interpreter path. This heals drift from app moves, manual
    venv changes, or (in theory) bundle path changes across updates.

    Clients where the user has disabled Deja (no ``deja`` entry) are
    left alone — we never resurrect a removed entry. Per-client
    exceptions are caught so one bad config doesn't break the others.
    """
    current_cmd = _python_path()
    for client in CLIENTS:
        try:
            if not client.detect_path.exists():
                continue
            if not client.config_path.exists():
                continue
            config = _read_config(client.config_path)
            servers = config.get(client.root_key, {}) if isinstance(config, dict) else {}
            if not isinstance(servers, dict):
                continue
            existing = servers.get(SERVER_NAME)
            if not isinstance(existing, dict):
                continue  # no deja entry — user disabled, don't touch
            old_cmd = existing.get("command")
            if old_cmd == current_cmd:
                continue  # already current
            # Rewrite only the command field; preserve args/type/env/etc.
            existing["command"] = current_cmd
            servers[SERVER_NAME] = existing
            config[client.root_key] = servers
            _write_config(client.config_path, config, client.create_dirs)
            log.info(
                "MCP auto-repair: updated %s (old=%s new=%s)",
                client.name,
                old_cmd,
                current_cmd,
            )
        except Exception as e:  # noqa: BLE001 — isolate per-client failures
            log.warning("MCP auto-repair: %s failed: %s", client.name, e)


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
