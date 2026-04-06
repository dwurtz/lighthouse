"""Lighthouse MCP server — exposes the personal wiki to Claude.

Turns Lighthouse into a persistent personal-context layer that any MCP
client (Claude Desktop, Claude Code, etc.) can query mid-conversation.
Claude can search the wiki, read/write pages, see recent observations,
and look up contacts — without the user re-explaining their life in
every conversation.

Start with:
    python -m lighthouse mcp          # stdio transport (what Claude expects)

Configure in Claude Desktop (~/Library/Application Support/Claude/claude_desktop_config.json):
    {
      "mcpServers": {
        "lighthouse": {
          "command": "/path/to/venv/bin/python",
          "args": ["-m", "lighthouse", "mcp"]
        }
      }
    }

Tools exposed:
    search_wiki         — semantic search across all wiki pages
    read_page           — full markdown content of one page
    write_page          — create or update a page
    delete_page         — remove a page
    list_pages          — catalog of everything in the wiki
    recent_observations — what the agent has seen recently
    user_profile        — who the user is (name, email, bio)
    search_contacts     — macOS contacts lookup
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
import mcp.types as types

log = logging.getLogger(__name__)

# Ensure the API key is in the environment before anything touches Gemini
from lighthouse.llm_client import _ensure_api_key_in_env
_ensure_api_key_in_env()


def create_server() -> Server:
    """Build and return a configured MCP Server instance.

    Each tool is a thin wrapper over an existing Lighthouse module — the
    wiki store, chat_tools, retriever, observation log, identity, and
    contacts. No new logic here, just MCP routing.
    """
    app = Server("lighthouse")

    # ------------------------------------------------------------------
    # Tools
    # ------------------------------------------------------------------

    @app.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name="search_wiki",
                description=(
                    "Semantic search across the user's personal wiki. Returns "
                    "the most relevant wiki page excerpts for a query. Use this "
                    "when you need context about a person, project, or topic in "
                    "the user's life."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Natural-language search query",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max pages to return (default 5)",
                            "default": 5,
                        },
                    },
                    "required": ["query"],
                },
            ),
            types.Tool(
                name="read_page",
                description=(
                    "Read the full markdown content of one wiki page, including "
                    "YAML frontmatter. Use this to get detailed info about a "
                    "specific person or project."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "category": {"type": "string", "enum": ["people", "projects"]},
                        "slug": {"type": "string", "description": "kebab-case page identifier"},
                    },
                    "required": ["category", "slug"],
                },
            ),
            types.Tool(
                name="write_page",
                description=(
                    "Create or overwrite a wiki page. Always read_page first if "
                    "the page might exist, so you preserve existing content you "
                    "don't mean to change. Requires a reason for the audit trail."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "category": {"type": "string", "enum": ["people", "projects"]},
                        "slug": {"type": "string"},
                        "content": {"type": "string", "description": "Full markdown body"},
                        "reason": {"type": "string", "description": "Why this change is being made"},
                    },
                    "required": ["category", "slug", "content", "reason"],
                },
            ),
            types.Tool(
                name="delete_page",
                description=(
                    "Remove a wiki page. Backed up via git so this is reversible. "
                    "Only delete when the user clearly asks."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "category": {"type": "string", "enum": ["people", "projects"]},
                        "slug": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["category", "slug", "reason"],
                },
            ),
            types.Tool(
                name="list_pages",
                description=(
                    "List every wiki page with its category, slug, and title. "
                    "Optionally filter by category. Call this to discover what "
                    "pages exist before searching or reading."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "category": {
                            "type": "string",
                            "enum": ["people", "projects"],
                            "description": "Optional filter",
                        },
                    },
                },
            ),
            types.Tool(
                name="recent_observations",
                description=(
                    "Return the user's recent digital activity — messages sent "
                    "and received, screenshots described, calendar events, "
                    "browser visits, etc. Use this for real-time context about "
                    "what the user has been doing."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "minutes": {
                            "type": "integer",
                            "description": "How many minutes of history (default 30, max 1440)",
                            "default": 30,
                        },
                        "source": {
                            "type": "string",
                            "description": "Filter to a specific source (imessage, whatsapp, email, screenshot, calendar, etc.)",
                        },
                    },
                },
            ),
            types.Tool(
                name="user_profile",
                description=(
                    "Return the user's identity — name, email, phone, and their "
                    "self-written bio from their wiki self-page. Use this to "
                    "understand who you're talking to."
                ),
                inputSchema={"type": "object", "properties": {}},
            ),
            types.Tool(
                name="search_contacts",
                description=(
                    "Search the user's macOS Contacts by name. Returns matching "
                    "contact names. Useful for resolving who someone is when the "
                    "user mentions a first name."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Name to search for"},
                    },
                    "required": ["query"],
                },
            ),
        ]

    @app.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
        try:
            result = _dispatch_tool(name, arguments)
            return [types.TextContent(type="text", text=result)]
        except Exception as e:
            log.exception("MCP tool %s failed", name)
            return [types.TextContent(type="text", text=f"error: {e}")]

    # ------------------------------------------------------------------
    # Resources
    # ------------------------------------------------------------------

    @app.list_resources()
    async def list_resources() -> list[types.Resource]:
        resources = [
            types.Resource(
                uri="lighthouse://index",
                name="Wiki Index",
                description="Auto-generated catalog of every person and project page",
                mimeType="text/markdown",
            ),
            types.Resource(
                uri="lighthouse://reflection",
                name="Reflection Notes",
                description="The agent's latest morning notes (what it noticed, what's stuck, questions for the user)",
                mimeType="text/markdown",
            ),
        ]
        # Add individual wiki pages as resources
        from lighthouse.config import WIKI_DIR
        for category in ("people", "projects"):
            cat_dir = WIKI_DIR / category
            if not cat_dir.is_dir():
                continue
            for path in sorted(cat_dir.glob("*.md")):
                if path.name.startswith((".", "_")):
                    continue
                slug = path.stem
                resources.append(types.Resource(
                    uri=f"lighthouse://wiki/{category}/{slug}",
                    name=f"{category}/{slug}",
                    description=f"Wiki page: {slug.replace('-', ' ').title()}",
                    mimeType="text/markdown",
                ))
        return resources

    @app.read_resource()
    async def read_resource(uri: str) -> str:
        from lighthouse.config import WIKI_DIR

        if uri == "lighthouse://index":
            index_path = WIKI_DIR / "index.md"
            return index_path.read_text() if index_path.exists() else "(no index yet)"

        if uri == "lighthouse://reflection":
            ref_path = WIKI_DIR / "reflection.md"
            return ref_path.read_text() if ref_path.exists() else "(no reflection notes yet)"

        if uri.startswith("lighthouse://wiki/"):
            parts = uri.replace("lighthouse://wiki/", "").split("/", 1)
            if len(parts) == 2:
                category, slug = parts
                path = WIKI_DIR / category / f"{slug}.md"
                if path.exists():
                    return path.read_text()
            return "(page not found)"

        return f"(unknown resource: {uri})"

    return app


# ---------------------------------------------------------------------------
# Tool dispatch — reuses existing Lighthouse modules
# ---------------------------------------------------------------------------

def _dispatch_tool(name: str, args: dict) -> str:
    """Route an MCP tool call to the right Lighthouse function.

    Returns a string (the tool result as text). Errors are returned as
    descriptive strings, not exceptions — so the LLM can see what went
    wrong and recover.
    """

    if name == "search_wiki":
        from lighthouse.llm.search import search as qmd_search
        query = args.get("query", "")
        limit = min(args.get("limit", 5), 20)
        result = qmd_search(query, limit=limit, collection="wiki")
        return result or "(no results)"

    if name == "read_page":
        from lighthouse.chat_tools import read_page
        r = read_page(args.get("category", ""), args.get("slug", ""))
        if r.ok and r.data:
            if r.data.get("exists"):
                return r.data["content"]
            return f"Page {args.get('category')}/{args.get('slug')} does not exist."
        return r.message

    if name == "write_page":
        from lighthouse.chat_tools import write_page
        r = write_page(
            args.get("category", ""),
            args.get("slug", ""),
            args.get("content", ""),
            args.get("reason", "MCP tool call"),
        )
        # Commit the change
        if r.ok:
            try:
                from lighthouse.wiki_git import commit_changes
                from lighthouse.wiki_catalog import rebuild_index
                rebuild_index()
                commit_changes(f"mcp: {r.message}")
            except Exception:
                pass
        return r.message

    if name == "delete_page":
        from lighthouse.chat_tools import delete_page
        r = delete_page(
            args.get("category", ""),
            args.get("slug", ""),
            args.get("reason", "MCP tool call"),
        )
        if r.ok:
            try:
                from lighthouse.wiki_git import commit_changes
                from lighthouse.wiki_catalog import rebuild_index
                rebuild_index()
                commit_changes(f"mcp: {r.message}")
            except Exception:
                pass
        return r.message

    if name == "list_pages":
        from lighthouse.chat_tools import list_pages
        r = list_pages(args.get("category"))
        if r.ok and r.data:
            pages = r.data["pages"]
            if not pages:
                return "(no pages)"
            lines = [f"- {p['category']}/{p['slug']} — {p['title']}" for p in pages]
            return "\n".join(lines)
        return r.message

    if name == "recent_observations":
        from lighthouse.config import OBSERVATIONS_LOG
        minutes = min(args.get("minutes", 30), 1440)
        source_filter = args.get("source")
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
        lines: list[str] = []
        if OBSERVATIONS_LOG.exists():
            for line in OBSERVATIONS_LOG.read_text().splitlines()[-500:]:
                try:
                    d = json.loads(line)
                    ts = datetime.fromisoformat(d["timestamp"])
                    if ts.tzinfo is None:
                        ts = ts.astimezone(timezone.utc)
                    if ts < cutoff:
                        continue
                    if source_filter and d.get("source") != source_filter:
                        continue
                    hm = ts.strftime("%H:%M")
                    lines.append(
                        f"[{hm}] [{d.get('source', '?')}] "
                        f"{d.get('sender', '?')}: {d.get('text', '')[:200]}"
                    )
                except (json.JSONDecodeError, KeyError, ValueError):
                    continue
        return "\n".join(lines[-100:]) or f"(no observations in the last {minutes} minutes)"

    if name == "user_profile":
        from lighthouse.identity import load_user
        user = load_user()
        return (
            f"Name: {user.name}\n"
            f"First name: {user.first_name}\n"
            f"Email: {user.email}\n"
            f"Phone: {user.phone}\n"
            f"\n{user.profile_md}"
        )

    if name == "search_contacts":
        query = args.get("query", "").lower().strip()
        if not query:
            return "(empty query)"
        from lighthouse.observations.contacts import _name_set, _build_index
        if _name_set is None:
            _build_index()
        names = _name_set or set()
        matches = [n for n in sorted(names) if query in n.lower()]
        if not matches:
            return f"(no contacts matching '{query}')"
        return "\n".join(matches[:20])

    return f"(unknown tool: {name})"


# ---------------------------------------------------------------------------
# Entry point — called by `lighthouse mcp`
# ---------------------------------------------------------------------------

async def run_mcp_server() -> None:
    """Start the MCP server over stdio.

    This is what Claude Desktop / Claude Code connects to. The transport
    is stdio (stdin/stdout) as required by the MCP protocol for subprocess
    servers. Logs go to stderr so they don't interfere with the protocol.
    """
    server = create_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())
