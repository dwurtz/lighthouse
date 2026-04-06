"""Lighthouse Context Engine — MCP server for Claude.

Exposes the user's personal wiki, observation stream, and contact graph
to any MCP client (Claude Desktop, Claude Code) as a persistent context
layer. Claude calls ``get_context(topic)`` at the start of any
conversation that touches people, projects, or commitments, and gets
back a pre-synthesized bundle of everything Lighthouse knows about that
topic — no need to manually search, paginate, or assemble the picture.

Three tools, not eight:

    get_context(topic)                — one call, full picture
    update_wiki(action, ...)          — write or delete a page
    recent_activity(minutes, source)  — raw observation stream

Start with:
    python -m lighthouse mcp

Configure in Claude Desktop:
    ~/Library/Application Support/Claude/claude_desktop_config.json
    {
      "mcpServers": {
        "lighthouse": {
          "command": "/path/to/venv/bin/python",
          "args": ["-m", "lighthouse", "mcp"]
        }
      }
    }
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
import mcp.types as types

log = logging.getLogger(__name__)

# Pre-populate the API key from keychain before any Gemini import
from lighthouse.llm_client import _ensure_api_key_in_env
_ensure_api_key_in_env()


# ---------------------------------------------------------------------------
# System-level instruction injected into every Claude session that has
# this MCP server connected. This is the key to proactive context use —
# without it, Claude treats the tools as optional and only calls them
# when it recognizes a gap. With it, Claude consults Lighthouse first.
# ---------------------------------------------------------------------------

_INSTRUCTIONS = """\
You have a personal context engine called Lighthouse connected. It \
maintains a living wiki about the user's people, projects, commitments, \
and recent digital activity (messages, emails, calendar, screenshots, \
browser history).

Before answering any question about a specific person, project, \
commitment, or recent event, call get_context(topic) first. The user \
has an extensive personal wiki that knows things you don't — their \
contacts, their projects, their recent messages, their commitments. \
Don't guess when you can look it up.

When the user asks you to remember something, update their wiki, or \
correct a fact about someone, use update_wiki to make the change \
directly. Every change is git-committed and reversible.

When the user asks "what have I been doing" or wants real-time context, \
use recent_activity to see their observation stream.\
"""


def create_server() -> Server:
    """Build and return a configured MCP Server instance."""
    app = Server(
        name="lighthouse",
        version="0.2.0",
        instructions=_INSTRUCTIONS,
    )

    # ------------------------------------------------------------------
    # Tools — three, designed around how Claude thinks
    # ------------------------------------------------------------------

    @app.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name="get_context",
                description=(
                    "Get personal context about a topic from the user's "
                    "Lighthouse wiki. Returns a synthesized bundle of relevant "
                    "wiki pages, the user's profile, recent observations "
                    "mentioning the topic, and related linked pages — all in "
                    "one call. ALWAYS call this before responding about a "
                    "specific person, project, commitment, event, or anything "
                    "that might be in the user's personal knowledge base. One "
                    "call here replaces what would otherwise be 5-6 manual "
                    "lookups. Topics can be a person's name, a project name, "
                    "a keyword, or a natural-language question."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "topic": {
                            "type": "string",
                            "description": (
                                "What you need context about — a person's name, "
                                "project name, keyword, or question. Examples: "
                                "'Amanda Peffer', 'soccer carpool', 'Palo Alto "
                                "relocation', 'what did I promise Sara'"
                            ),
                        },
                    },
                    "required": ["topic"],
                },
            ),
            types.Tool(
                name="update_wiki",
                description=(
                    "Create, update, or delete a page in the user's personal "
                    "wiki. Use this when the user asks you to remember "
                    "something, correct a fact, add a person, or remove a "
                    "page. Always call get_context first to read the existing "
                    "page before overwriting — preserve YAML frontmatter and "
                    "content you didn't mean to change. Every change is "
                    "git-committed and reversible."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["write", "delete"],
                            "description": "'write' to create/update, 'delete' to remove",
                        },
                        "category": {
                            "type": "string",
                            "enum": ["people", "projects"],
                        },
                        "slug": {
                            "type": "string",
                            "description": "kebab-case page identifier (e.g. 'amanda-peffer')",
                        },
                        "content": {
                            "type": "string",
                            "description": "Full markdown body including YAML frontmatter (required for 'write', ignored for 'delete')",
                        },
                        "reason": {
                            "type": "string",
                            "description": "Why this change is being made — becomes the git commit message",
                        },
                    },
                    "required": ["action", "category", "slug", "reason"],
                },
            ),
            types.Tool(
                name="recent_activity",
                description=(
                    "See what the user has been doing recently — messages "
                    "sent and received, screenshots described, calendar "
                    "events, browser visits, clipboard copies, voice notes. "
                    "Use this when the user asks 'what have I been doing', "
                    "'what's on my screen', or when you need real-time "
                    "context about their current activity."
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
                            "description": (
                                "Filter to a specific source: imessage, whatsapp, "
                                "email, screenshot, calendar, clipboard, chat, "
                                "browser, drive, tasks. Omit for all sources."
                            ),
                        },
                    },
                },
            ),
        ]

    @app.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
        try:
            result = _dispatch(name, arguments)
            return [types.TextContent(type="text", text=result)]
        except Exception as e:
            log.exception("MCP tool %s failed", name)
            return [types.TextContent(type="text", text=f"error: {e}")]

    # ------------------------------------------------------------------
    # Resources — wiki pages readable directly by Claude
    # ------------------------------------------------------------------

    @app.list_resources()
    async def list_resources() -> list[types.Resource]:
        resources = [
            types.Resource(
                uri="lighthouse://index",
                name="Wiki Index",
                description="Catalog of every person and project page",
                mimeType="text/markdown",
            ),
            types.Resource(
                uri="lighthouse://reflection",
                name="Reflection Notes",
                description="The agent's latest morning notes",
                mimeType="text/markdown",
            ),
        ]
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
                    description=slug.replace("-", " ").title(),
                    mimeType="text/markdown",
                ))
        return resources

    @app.read_resource()
    async def read_resource(uri: str) -> str:
        from lighthouse.config import WIKI_DIR
        if uri == "lighthouse://index":
            p = WIKI_DIR / "index.md"
            return p.read_text() if p.exists() else "(no index)"
        if uri == "lighthouse://reflection":
            p = WIKI_DIR / "reflection.md"
            return p.read_text() if p.exists() else "(no reflection notes)"
        if uri.startswith("lighthouse://wiki/"):
            parts = uri.replace("lighthouse://wiki/", "").split("/", 1)
            if len(parts) == 2:
                p = WIKI_DIR / parts[0] / f"{parts[1]}.md"
                if p.exists():
                    return p.read_text()
            return "(page not found)"
        return f"(unknown resource: {uri})"

    return app


# ---------------------------------------------------------------------------
# get_context — the core synthesis tool
# ---------------------------------------------------------------------------

_WIKILINK_RE = re.compile(r"\[\[([^\]\n|]+?)(?:\|[^\]\n]*)?\]\]")


def _get_context(topic: str) -> str:
    """Synthesize personal context for a topic from multiple sources.

    Does in ONE call what would otherwise take 5-6 separate tool calls:
    1. Semantic wiki search for the topic
    2. User profile
    3. Recent observations mentioning the topic
    4. Related pages (one-hop wiki-link traversal from matched pages)

    Returns a structured markdown bundle Claude can consume directly.
    """
    from lighthouse.config import WIKI_DIR, OBSERVATIONS_LOG
    from lighthouse.identity import load_user

    sections: list[str] = []

    # --- User profile (always included, cheap) ---
    user = load_user()
    sections.append(
        f"## Who the user is\n\n"
        f"**{user.name}** ({user.email})\n\n"
        f"{user.profile_md.strip()}"
    )

    # --- Wiki search for the topic ---
    matched_pages: list[dict] = []
    matched_slugs: set[str] = set()
    try:
        from lighthouse.llm.search import search as qmd_search
        search_result = qmd_search(topic, limit=5, collection="wiki")
        if search_result:
            sections.append(f"## Wiki pages matching \"{topic}\"\n\n{search_result}")
            # Extract slugs from the search result for link traversal
            # QMD returns formatted text with page headers like "### category/slug"
            for match in re.finditer(r"###\s+(\w+)/([a-z0-9-]+)", search_result):
                cat, slug = match.group(1), match.group(2)
                matched_slugs.add(slug)
                path = WIKI_DIR / cat / f"{slug}.md"
                if path.exists():
                    matched_pages.append({
                        "category": cat,
                        "slug": slug,
                        "content": path.read_text(),
                    })
    except Exception:
        log.debug("QMD search failed", exc_info=True)

    # Fallback: if QMD returned nothing, try a direct slug/title match
    if not matched_pages:
        topic_slug = re.sub(r"[^a-z0-9]+", "-", topic.lower()).strip("-")
        topic_lower = topic.lower()
        for category in ("people", "projects"):
            cat_dir = WIKI_DIR / category
            if not cat_dir.is_dir():
                continue
            for path in cat_dir.glob("*.md"):
                slug = path.stem
                if topic_slug in slug or topic_lower in slug.replace("-", " "):
                    try:
                        content = path.read_text()
                        matched_pages.append({
                            "category": category,
                            "slug": slug,
                            "content": content,
                        })
                        matched_slugs.add(slug)
                    except OSError:
                        continue

        if matched_pages:
            page_text = "\n\n".join(
                f"### {p['category']}/{p['slug']}\n\n{p['content'].strip()}"
                for p in matched_pages[:5]
            )
            sections.append(f"## Wiki pages matching \"{topic}\"\n\n{page_text}")

    if not matched_pages:
        sections.append(
            f"## Wiki pages matching \"{topic}\"\n\n"
            f"(no pages found — the user may not have a wiki entry for this topic yet)"
        )

    # --- Related pages (one-hop link traversal) ---
    linked_slugs: set[str] = set()
    for page in matched_pages:
        for m in _WIKILINK_RE.finditer(page["content"]):
            link_target = m.group(1).strip().lower().replace(" ", "-")
            if link_target not in matched_slugs:
                linked_slugs.add(link_target)

    if linked_slugs:
        related: list[str] = []
        for slug in sorted(linked_slugs)[:10]:
            for category in ("people", "projects"):
                path = WIKI_DIR / category / f"{slug}.md"
                if path.exists():
                    try:
                        content = path.read_text()
                        # Just the first paragraph, not the full page
                        lines = [l for l in content.split("\n") if l.strip() and not l.startswith("---") and not l.startswith("#")]
                        summary = lines[0][:200] if lines else "(empty)"
                        related.append(f"- **{category}/{slug}**: {summary}")
                    except OSError:
                        continue
                    break
        if related:
            sections.append("## Related pages (linked from the above)\n\n" + "\n".join(related))

    # --- Recent observations mentioning the topic ---
    if OBSERVATIONS_LOG.exists():
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        topic_words = set(topic.lower().split())
        matching_obs: list[str] = []
        try:
            for line in OBSERVATIONS_LOG.read_text().splitlines()[-1000:]:
                try:
                    d = json.loads(line)
                    ts = datetime.fromisoformat(d["timestamp"])
                    if ts.tzinfo is None:
                        ts = ts.astimezone(timezone.utc)
                    if ts < cutoff:
                        continue
                    text_lower = (d.get("text", "") + " " + d.get("sender", "")).lower()
                    if any(w in text_lower for w in topic_words):
                        hm = ts.strftime("%H:%M")
                        matching_obs.append(
                            f"[{hm}] [{d.get('source', '?')}] "
                            f"{d.get('sender', '?')}: {d.get('text', '')[:200]}"
                        )
                except (json.JSONDecodeError, KeyError, ValueError):
                    continue
        except OSError:
            pass

        if matching_obs:
            obs_text = "\n".join(matching_obs[-20:])
            sections.append(f"## Recent activity mentioning \"{topic}\" (last 24h)\n\n{obs_text}")

    return "\n\n---\n\n".join(sections)


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------

def _dispatch(name: str, args: dict) -> str:
    if name == "get_context":
        topic = args.get("topic", "")
        if not topic:
            return "(empty topic — tell me what you need context about)"
        return _get_context(topic)

    if name == "update_wiki":
        action = args.get("action", "write")
        category = args.get("category", "")
        slug = args.get("slug", "")
        reason = args.get("reason", "MCP update")

        if action == "delete":
            from lighthouse.chat_tools import delete_page
            r = delete_page(category, slug, reason)
        else:
            content = args.get("content", "")
            from lighthouse.chat_tools import write_page
            r = write_page(category, slug, content, reason)

        if r.ok:
            try:
                from lighthouse.wiki_git import commit_changes
                from lighthouse.wiki_catalog import rebuild_index
                rebuild_index()
                commit_changes(f"mcp: {r.message}")
            except Exception:
                pass
        return r.message

    if name == "recent_activity":
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

    return f"(unknown tool: {name})"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def run_mcp_server() -> None:
    """Start the MCP server over stdio for Claude Desktop / Code."""
    server = create_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )
