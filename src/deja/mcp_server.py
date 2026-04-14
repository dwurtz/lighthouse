"""Deja Context Engine — MCP server for Claude.

Exposes the user's personal wiki, observation stream, and contact graph
to any MCP client (Claude Desktop, Claude Code) as a persistent context
layer. Claude calls ``get_context(topic)`` at the start of any
conversation that touches people, projects, or commitments, and gets
back a pre-synthesized bundle of everything Deja knows about that
topic — no need to manually search, paginate, or assemble the picture.

Three tools, not eight:

    get_context(topic)                — one call, full picture
    update_wiki(action, ...)          — write or delete a page
    recent_activity(minutes, source)  — raw observation stream

Start with:
    python -m deja mcp

Configure in Claude Desktop:
    ~/Library/Application Support/Claude/claude_desktop_config.json
    {
      "mcpServers": {
        "deja": {
          "command": "/path/to/venv/bin/python",
          "args": ["-m", "deja", "mcp"]
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



# ---------------------------------------------------------------------------
# System-level instruction injected into every Claude session that has
# this MCP server connected. This is the key to proactive context use —
# without it, Claude treats the tools as optional and only calls them
# when it recognizes a gap. With it, Claude consults Deja first.
# ---------------------------------------------------------------------------

_INSTRUCTIONS = """\
You have a personal context engine called Deja connected. It \
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
        name="deja",
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
                    "Deja wiki. Returns a synthesized bundle of relevant "
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
                    "something, correct a fact, add a person or project, "
                    "record an event that happened, or remove a page. "
                    "Always call get_context first to read the existing "
                    "page before overwriting — preserve YAML frontmatter and "
                    "content you didn't mean to change. Every change is "
                    "git-committed and reversible.\n\n"
                    "Three wiki categories:\n"
                    "  • people — one page per real person. Describes WHO "
                    "they are (current state) in flowing prose.\n"
                    "  • projects — one page per ongoing project, goal, "
                    "or life thread. Describes WHAT it is (current state).\n"
                    "  • events — timestamped record of what happened. "
                    "Events have a date-prefixed slug of the form "
                    "'YYYY-MM-DD/event-name' and structured YAML "
                    "frontmatter (date, time, people, projects). They "
                    "link back to the people and projects involved. "
                    "Entity pages describe state; events describe what "
                    "happened."
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
                            "enum": ["people", "projects", "events"],
                        },
                        "slug": {
                            "type": "string",
                            "description": (
                                "kebab-case page identifier. For people and "
                                "projects: just the name, e.g. 'amanda-peffer'. "
                                "For events: date-prefixed, e.g. "
                                "'2026-04-07/david-invited-to-llm-kinsol-update'."
                            ),
                        },
                        "content": {
                            "type": "string",
                            "description": (
                                "Full markdown body including YAML frontmatter "
                                "(required for 'write', ignored for 'delete'). "
                                "For events, frontmatter must include date, "
                                "time (quoted string), people (list of slugs), "
                                "and projects (list of slugs). The opening "
                                "'---' fence must be on its own line; no blank "
                                "lines inside the frontmatter block."
                            ),
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
                uri="deja://index",
                name="Wiki Index",
                description="Catalog of every person and project page",
                mimeType="text/markdown",
            ),
            types.Resource(
                uri="deja://reflection",
                name="Reflection Notes",
                description="The agent's latest morning notes",
                mimeType="text/markdown",
            ),
        ]
        from deja.config import WIKI_DIR
        for category in ("people", "projects"):
            cat_dir = WIKI_DIR / category
            if not cat_dir.is_dir():
                continue
            for path in sorted(cat_dir.glob("*.md")):
                if path.name.startswith((".", "_")):
                    continue
                slug = path.stem
                resources.append(types.Resource(
                    uri=f"deja://wiki/{category}/{slug}",
                    name=f"{category}/{slug}",
                    description=slug.replace("-", " ").title(),
                    mimeType="text/markdown",
                ))
        return resources

    @app.read_resource()
    async def read_resource(uri: str) -> str:
        from deja.config import WIKI_DIR
        if uri == "deja://index":
            p = WIKI_DIR / "index.md"
            return p.read_text() if p.exists() else "(no index)"
        if uri == "deja://reflection":
            p = WIKI_DIR / "reflection.md"
            return p.read_text() if p.exists() else "(no reflection notes)"
        if uri.startswith("deja://wiki/"):
            parts = uri.replace("deja://wiki/", "").split("/", 1)
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


def _goals_for_topic(topic: str) -> str:
    """Return the slice of goals.md whose lines mention ``topic``.

    Walks Tasks, Waiting for, and Reminders sections and keeps any
    bullet whose text (case-insensitive) contains the topic or any
    topic word. Returns a markdown fragment grouped by section, or an
    empty string if nothing matches. Pure file read — no LLM, no
    retrieval.
    """
    from deja.goals import GOALS_PATH, _parse_sections
    if not GOALS_PATH.exists():
        return ""
    try:
        text = GOALS_PATH.read_text(encoding="utf-8")
    except OSError:
        return ""

    topic_lower = topic.lower().strip()
    if not topic_lower:
        return ""
    topic_words = [w for w in re.split(r"\s+", topic_lower) if len(w) >= 3]

    _, sections = _parse_sections(text)
    out_parts: list[str] = []
    for section_name in ("Tasks", "Waiting for", "Reminders"):
        lines = sections.get(section_name, [])
        hits: list[str] = []
        for line in lines:
            stripped = line.lstrip()
            if not stripped.startswith("- "):
                continue
            low = line.lower()
            if topic_lower in low or any(w in low for w in topic_words):
                hits.append(line.rstrip())
        if hits:
            out_parts.append(f"### {section_name}\n" + "\n".join(hits))
    return "\n\n".join(out_parts)


def _qmd_query(topic: str, collection: str | None = None, limit: int = 5) -> str:
    """Run a BM25 search against the wiki via ``qmd search``.

    Deliberately NOT ``qmd query`` — that path runs HyDE rerank, which
    issues an LLM call per search and takes ~10s on this wiki. BM25
    alone scores named-entity matches at 85%+ (Amanda → amanda-peffer.md)
    and returns in ~0.3s. HyDE's conceptual-query edge isn't worth the
    30x latency for any caller we have today: command classification,
    query synthesis, and MCP get_context all need fast entity lookup,
    not fuzzy conceptual retrieval.

    Raises ``RuntimeError`` on any failure so callers surface the
    problem instead of silently running against a blank wiki — that
    was the root cause of the "draft email to Amanda" dispatch
    failing with missing ``to``.
    """
    import subprocess

    cmd = ["qmd", "search", topic, "-n", str(limit)]
    if collection:
        cmd += ["-c", collection]

    r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        raise RuntimeError(
            f"qmd search failed (rc={r.returncode}): "
            f"{r.stderr[:400] or '(no stderr)'}"
        )
    return (r.stdout or "").strip()


def _get_context(topic: str) -> str:
    """Synthesize personal context for a topic from multiple sources.

    Uses QMD's hybrid search (BM25 + vector + HyDE) across the entire
    Deja wiki — people, projects, AND events — in a single query.
    QMD returns the most semantically relevant chunks regardless of
    category, so a search for "Amanda Peffer" can return her person page,
    the Blade & Rose project page, AND timestamped event pages like
    "amanda-shared-sales-data" — all ranked by relevance.

    Also includes:
    - User profile (always)
    - One-hop wiki-link traversal for related entities
    - Recent raw observations (last 60 min) for real-time context

    Returns a structured markdown bundle Claude can consume directly.
    """
    from deja.config import WIKI_DIR, OBSERVATIONS_LOG
    from deja.identity import load_user

    sections: list[str] = []

    # --- User profile (always included, cheap) ---
    user = load_user()
    sections.append(
        f"## Who the user is\n\n"
        f"**{user.name}** ({user.email})\n\n"
        f"{user.profile_md.strip()}"
    )

    # --- QMD hybrid search across wiki + events ---
    from deja.config import QMD_COLLECTION
    qmd_result = _qmd_query(topic, collection=QMD_COLLECTION, limit=8)
    if qmd_result:
        sections.append(f"## Relevant pages and events for \"{topic}\"\n\n{qmd_result}")
    else:
        # Fallback: direct slug/title match
        topic_slug = re.sub(r"[^a-z0-9]+", "-", topic.lower()).strip("-")
        topic_lower = topic.lower()
        fallback_pages: list[str] = []
        for category in ("people", "projects"):
            cat_dir = WIKI_DIR / category
            if not cat_dir.is_dir():
                continue
            for path in cat_dir.glob("*.md"):
                slug = path.stem
                if topic_slug in slug or topic_lower in slug.replace("-", " "):
                    try:
                        content = path.read_text()
                        fallback_pages.append(f"### {category}/{slug}\n\n{content.strip()}")
                    except OSError:
                        continue
        if fallback_pages:
            sections.append(f"## Wiki pages matching \"{topic}\"\n\n" + "\n\n".join(fallback_pages[:5]))
        else:
            sections.append(
                f"## Wiki pages matching \"{topic}\"\n\n"
                f"(no pages found — the user may not have a wiki entry for this topic yet)"
            )

    # --- Open commitments touching this topic from goals.md ---
    try:
        goals_slice = _goals_for_topic(topic)
        if goals_slice:
            sections.append(
                f"## Open commitments touching \"{topic}\"\n\n{goals_slice}"
            )
    except Exception:
        log.debug("goals slice failed", exc_info=True)

    # --- Recent raw observations (last 60 min — real-time layer) ---
    if OBSERVATIONS_LOG.exists():
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=60)
        topic_words = set(topic.lower().split())
        matching_obs: list[str] = []
        try:
            for line in OBSERVATIONS_LOG.read_text().splitlines()[-500:]:
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
            sections.append(
                f"## Live activity mentioning \"{topic}\" (last hour)\n\n"
                + "\n".join(matching_obs[-15:])
            )

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
            from deja.chat_tools import delete_page
            r = delete_page(category, slug, reason)
        else:
            content = args.get("content", "")
            from deja.chat_tools import write_page
            r = write_page(category, slug, content, reason)

        if r.ok:
            try:
                from deja.wiki_git import commit_changes
                from deja.wiki_catalog import rebuild_index
                rebuild_index()
                commit_changes(f"mcp: {r.message}")
            except Exception:
                pass
        return r.message

    if name == "recent_activity":
        from deja.config import OBSERVATIONS_LOG
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
