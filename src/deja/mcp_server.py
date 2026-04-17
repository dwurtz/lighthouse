"""Deja Context Engine — MCP server.

Exposes the user's personal wiki, observation stream, goals list, and
action layer to any MCP client (Claude Desktop, Claude Code, Hermes) as
a persistent context-and-action layer. The agent calls into Deja to
read what's happening, write what it learns, and take action in the
world on the user's behalf.

Two client profiles:

  * **Claude Desktop / Code** — the original three tools (``get_context``,
    ``update_wiki``, ``recent_activity``) cover conversational Q&A.
  * **Hermes** — the chief-of-staff surface: ``daily_briefing``,
    ``search_deja``, goal-mutation tools, and ``execute_action``. Hermes
    opens each loop with ``daily_briefing``, then reads, decides, writes.

Every mutating call sets the audit context to
``trigger=("mcp","hermes")`` (or the calling client) so each change is
traceable via ``python -m deja hermes-trail`` or raw
``~/.deja/audit.jsonl``.

Start with:
    python -m deja mcp

Configure in Hermes (``~/.hermes/config.yaml``):
    mcp_servers:
      deja:
        command: /Users/wurtz/projects/deja/.venv/bin/python
        args: ["-m", "deja", "mcp"]

Configure in Claude Desktop:
    ~/Library/Application Support/Claude/claude_desktop_config.json
    { "mcpServers": { "deja": { "command": "...", "args": ["-m","deja","mcp"] } } }
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
Deja is the user's personal memory and action layer. It continuously \
observes their digital activity (messages, emails, calendar, browser, \
screenshots) and maintains a living wiki: **people** (who they know), \
**projects** (ongoing arcs), **events** (timestamped record of what \
happened), and a **goals.md** with their open tasks, who they're \
waiting on, and scheduled reminders. Deja can also act in the world on \
their behalf — drafting emails, creating calendar events and tasks.

You are the user's chief of staff. Your job is to serve their goals. \
To do that well, start by knowing what's going on, then decide what \
needs doing, then do it.

**How to work:**

1. Begin loops with `daily_briefing` — one call returns the user's \
profile, today's open tasks, who owes them what (waiting-fors), due \
reminders, active projects, and recent events. This is your context \
foundation every time you wake up.

2. For targeted questions, `search_deja(query)` searches across \
people, projects, events, and goals in one pass. Use it before guessing \
anything about someone or something. Follow up with `get_page` for the \
full content of a specific hit.

3. When you learn something worth remembering — someone committed to \
something, a fact changed, an arc moved — write it. `update_wiki` for \
people/projects/events. `add_task` / `add_waiting_for` / `add_reminder` \
for goals. Always pass a concrete `reason` so the audit trail is \
readable.

4. When something needs doing in the real world, call `execute_action` \
with the action type (`draft_email`, `calendar_create`, `create_task`, \
etc.) and params. Drafts require the user's review before sending — \
that's a feature, not a limit.

5. Close loops as you discover evidence: `complete_task` when a task \
got done, `resolve_waiting_for` when someone delivered, \
`resolve_reminder` when you've answered the question you set for \
yourself. Leaving stale items pending is a failure mode.

Never guess when you can look up. Prefer tool calls over freeform \
recall. Every write leaves an audit entry the user can review.\
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
            # ---------------- Hermes chief-of-staff surface ----------------
            types.Tool(
                name="daily_briefing",
                description=(
                    "One call returns everything you need to start a loop: "
                    "the user's profile, today's date, their open Tasks, "
                    "who they're Waiting for, due + upcoming Reminders, "
                    "projects with activity in the last 7 days, and recent "
                    "events from the last 24 hours. Always begin a work "
                    "loop with this."
                ),
                inputSchema={"type": "object", "properties": {}},
            ),
            types.Tool(
                name="search_deja",
                description=(
                    "Universal search across the user's entire Deja memory — "
                    "people, projects, events, AND their open tasks / "
                    "waiting-fors / reminders in goals.md. Returns ranked "
                    "hits with category labels. Use this whenever you need "
                    "to find a person, a project, a past event, or an open "
                    "commitment. When you already know a specific page "
                    "slug, use get_page for the full content instead."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer", "default": 10},
                    },
                    "required": ["query"],
                },
            ),
            types.Tool(
                name="get_page",
                description=(
                    "Read one wiki page in full by category + slug. Call "
                    "this after search_deja when you need the complete "
                    "content of a specific hit."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "category": {"type": "string", "enum": ["people", "projects", "events"]},
                        "slug": {
                            "type": "string",
                            "description": (
                                "For people/projects: the kebab slug "
                                "('jon-sturos'). For events: 'YYYY-MM-DD/slug'."
                            ),
                        },
                    },
                    "required": ["category", "slug"],
                },
            ),
            types.Tool(
                name="list_goals",
                description=(
                    "Return the raw structured contents of goals.md grouped "
                    "by section: Standing context, Automations, Tasks, "
                    "Waiting for, Reminders. Use when you need the full "
                    "goal state rather than a ranked search slice."
                ),
                inputSchema={"type": "object", "properties": {}},
            ),
            types.Tool(
                name="search_events",
                description=(
                    "Search timestamped event pages only (events/YYYY-MM-DD/). "
                    "Scoped to last N days with optional person/project "
                    "filters. Use for questions like 'what happened with "
                    "Jon this week' or 'activity on home-roof'."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "days": {"type": "integer", "default": 7},
                        "person": {
                            "type": "string",
                            "description": "Person slug to filter by (e.g. 'jon-sturos')",
                        },
                        "project": {
                            "type": "string",
                            "description": "Project slug to filter by (e.g. 'home-roof')",
                        },
                    },
                },
            ),
            types.Tool(
                name="add_task",
                description=(
                    "Add a new item to the user's Tasks list. Use when the "
                    "user commits to something or when you decide a "
                    "recurring check belongs in their attention."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "description": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["description", "reason"],
                },
            ),
            types.Tool(
                name="complete_task",
                description=(
                    "Mark a task done. Substring match against the task "
                    "line. Call ONLY when evidence confirms it happened."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "needle": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["needle", "reason"],
                },
            ),
            types.Tool(
                name="archive_task",
                description="Archive a task no longer relevant (not completed). Substring match.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "needle": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["needle", "reason"],
                },
            ),
            types.Tool(
                name="add_waiting_for",
                description=(
                    "Record that someone owes the user something. Rendered "
                    "as '**[[person-slug|Person Name]]** — what they owe'. "
                    "Auto-expires after 21 days; archive explicitly sooner "
                    "if the thread dies."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "person_slug": {"type": "string"},
                        "person_name": {"type": "string"},
                        "what": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["person_name", "what", "reason"],
                },
            ),
            types.Tool(
                name="resolve_waiting_for",
                description="Mark a waiting-for resolved (the person delivered). Substring match.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "needle": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["needle", "reason"],
                },
            ),
            types.Tool(
                name="archive_waiting_for",
                description="Archive a waiting-for no longer relevant. Substring match.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "needle": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["needle", "reason"],
                },
            ),
            types.Tool(
                name="add_reminder",
                description=(
                    "Schedule a future check-in for yourself. 'date' is "
                    "strict YYYY-MM-DD. 'topics' is a list of wiki slugs "
                    "this reminder touches (used for retrieval)."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "date": {"type": "string"},
                        "question": {"type": "string"},
                        "topics": {"type": "array", "items": {"type": "string"}, "default": []},
                        "reason": {"type": "string"},
                    },
                    "required": ["date", "question", "reason"],
                },
            ),
            types.Tool(
                name="resolve_reminder",
                description="Mark a reminder answered. Substring match on question text.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "needle": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["needle", "reason"],
                },
            ),
            types.Tool(
                name="archive_reminder",
                description="Archive a reminder no longer relevant (moot).",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "needle": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["needle", "reason"],
                },
            ),
            types.Tool(
                name="execute_action",
                description=(
                    "Take action in the real world via Deja's action layer. "
                    "Types:\n"
                    "  • draft_email — {to, subject, body}. Creates a Gmail "
                    "draft for the user to review and send.\n"
                    "  • calendar_create — {summary, start, end, attendees?, "
                    "description?, location?}. ISO 8601 datetimes.\n"
                    "  • calendar_update — {event_id, ...patch}.\n"
                    "  • create_task — {title, notes?, due?}. Google Tasks.\n"
                    "  • complete_task — {task_id} or {title}.\n"
                    "  • notify — {title, body}. macOS banner."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": [
                                "draft_email",
                                "calendar_create",
                                "calendar_update",
                                "create_task",
                                "complete_task",
                                "notify",
                            ],
                        },
                        "params": {"type": "object"},
                        "reason": {"type": "string"},
                    },
                    "required": ["type", "params", "reason"],
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
# Hermes chief-of-staff handlers
# ---------------------------------------------------------------------------


def _mcp_audit_context() -> None:
    """Stamp audit context so writes this turn are tagged trigger=mcp/hermes."""
    from deja import audit
    audit.set_context(cycle="", trigger_kind="mcp", trigger_detail="hermes")


def _profile_headline(profile_md: str) -> str:
    """Trim the user profile to its first paragraph.

    The full david-wurtz.md body is ~1.5K words and shows up on every
    briefing call. Most of it is stable state the agent already knows.
    The first paragraph carries who-they-are + current-affiliation,
    which is what changes rarely but matters most for framing.
    """
    text = profile_md.strip()
    if not text:
        return ""
    # First paragraph = up to the first blank line
    for i, line in enumerate(text.splitlines()):
        if not line.strip() and i > 0:
            return "\n".join(text.splitlines()[:i]).strip()
    return text


def _recent_narratives(limit: int = 5) -> list[str]:
    """Return the last N observation narrative entries from today's file.

    Narratives live in ``~/Deja/observations/YYYY-MM-DD.md``, one block
    per integrate cycle, separated by ``\\n\\n---\\n\\n``. Each block
    leads with ``## HH:MM:SS`` then prose. We return the most recent
    ``limit`` blocks as-is so the agent sees prose summaries of what
    just happened, not raw slug lists.
    """
    from deja.config import WIKI_DIR

    obs_file = WIKI_DIR / "observations" / f"{datetime.now().strftime('%Y-%m-%d')}.md"
    if not obs_file.exists():
        return []
    try:
        text = obs_file.read_text(encoding="utf-8")
    except OSError:
        return []
    blocks = [b.strip() for b in text.split("\n\n---\n\n") if b.strip()]
    return blocks[-limit:]


def _daily_briefing() -> str:
    """Compose the one-call briefing an agent opens every loop with.

    Five sections:
      1. Date — today's weekday/date + wall time
      2. User — first paragraph of the user's wiki page (headline only)
      3. Tasks / Waiting for / Reminders — raw bullets from goals.md
      4. Active projects — pages modified in last 7 days
      5. Recent activity — last ~5 observation narratives (prose)

    Narratives replace the old "Events in last 24h" slug list — prose
    from the integrate loop is denser than bare filenames.
    """
    from deja.config import WIKI_DIR
    from deja.identity import load_user
    from deja.goals import GOALS_PATH, _parse_sections

    today = datetime.now()
    out: list[str] = []

    user = load_user()
    out.append(
        f"## Date\n\n{today.strftime('%A, %B %-d, %Y')} ({today.strftime('%H:%M')})\n"
    )
    out.append(
        f"## User\n\n**{user.name}** ({user.email})\n\n{_profile_headline(user.profile_md)}"
    )

    # Goals slice — Tasks, Waiting for, Reminders
    if GOALS_PATH.exists():
        _, sections = _parse_sections(GOALS_PATH.read_text(encoding="utf-8"))
        for section_name in ("Tasks", "Waiting for", "Reminders"):
            bullets = [
                ln.rstrip()
                for ln in sections.get(section_name, [])
                if ln.lstrip().startswith("- ")
            ]
            if bullets:
                out.append(f"## {section_name}\n\n" + "\n".join(bullets))
            else:
                out.append(f"## {section_name}\n\n(none)")

    # Active projects — any project page modified in the last 7 days
    week_ago = today.timestamp() - 7 * 86400
    projects_dir = WIKI_DIR / "projects"
    if projects_dir.is_dir():
        active: list[tuple[float, str, str]] = []
        for path in projects_dir.glob("*.md"):
            try:
                mtime = path.stat().st_mtime
                if mtime < week_ago:
                    continue
                title = path.stem
                try:
                    body = path.read_text(encoding="utf-8")
                    m = re.search(r"^# (.+)$", body, re.MULTILINE)
                    if m:
                        title = m.group(1).strip()
                except OSError:
                    pass
                first_sentence = ""
                try:
                    body_after_h1 = re.sub(r"(?s)^---.*?---\s*", "", body)
                    body_after_h1 = re.sub(r"(?m)^#.*\n", "", body_after_h1).strip()
                    first_sentence = body_after_h1.split(".")[0][:200]
                except Exception:
                    pass
                active.append((mtime, title, first_sentence))
            except OSError:
                continue
        active.sort(reverse=True)
        if active:
            out.append(
                "## Active projects (last 7 days)\n\n"
                + "\n".join(f"- **{t}** — {s}" for _, t, s in active[:12])
            )

    # Recent activity — prose narratives from today's observations file.
    # Denser than the legacy event-slug list: the integrate loop already
    # summarizes "what happened" each cycle, so we just surface its
    # voice to the agent instead of re-inventing the wheel.
    narratives = _recent_narratives(limit=5)
    if narratives:
        out.append(
            "## Recent activity (last cycles)\n\n" + "\n\n".join(narratives)
        )

    return "\n\n---\n\n".join(out)


def _search_deja(query: str, limit: int = 10) -> str:
    """Universal search — QMD across wiki + goals.md slice."""
    if not query:
        return "(empty query — what are you searching for?)"

    from deja.config import QMD_COLLECTION

    sections: list[str] = []

    try:
        wiki_hits = _qmd_query(query, collection=QMD_COLLECTION, limit=limit)
        if wiki_hits.strip():
            sections.append(f"## Wiki hits (people / projects / events)\n\n{wiki_hits}")
    except Exception as e:
        sections.append(f"## Wiki search error\n\n{e}")

    goals_slice = _goals_for_topic(query)
    if goals_slice:
        sections.append(f"## Open commitments touching '{query}'\n\n{goals_slice}")

    if not sections:
        return f"(no hits for '{query}')"
    return "\n\n---\n\n".join(sections)


def _get_page(category: str, slug: str) -> str:
    """Read one wiki page by category + slug."""
    from deja.config import WIKI_DIR
    if category not in ("people", "projects", "events"):
        return f"(unknown category: {category})"
    if category == "events" and "/" in slug:
        path = WIKI_DIR / "events" / f"{slug}.md"
    else:
        path = WIKI_DIR / category / f"{slug}.md"
    if not path.exists():
        return f"(page not found: {category}/{slug})"
    try:
        return path.read_text(encoding="utf-8")
    except OSError as e:
        return f"(read failed: {e})"


def _list_goals() -> str:
    """Return goals.md sections as structured markdown."""
    from deja.goals import GOALS_PATH, _parse_sections
    if not GOALS_PATH.exists():
        return "(goals.md not found)"
    _, sections = _parse_sections(GOALS_PATH.read_text(encoding="utf-8"))
    out: list[str] = []
    for name in ("Standing context", "Automations", "Tasks", "Waiting for", "Reminders"):
        lines = sections.get(name, [])
        body = "\n".join(ln.rstrip() for ln in lines if ln.rstrip())
        out.append(f"## {name}\n\n{body or '(none)'}")
    return "\n\n".join(out)


def _search_events(
    query: str = "",
    days: int = 7,
    person: str | None = None,
    project: str | None = None,
) -> str:
    """Event-only search with date + person/project filters."""
    from deja.config import WIKI_DIR

    events_dir = WIKI_DIR / "events"
    if not events_dir.is_dir():
        return "(no events directory)"

    cutoff = datetime.now() - timedelta(days=max(1, days))
    q = (query or "").lower().strip()

    hits: list[tuple[str, str, str]] = []  # (date, slug, excerpt)
    for day_dir in sorted(events_dir.iterdir(), reverse=True):
        if not day_dir.is_dir():
            continue
        try:
            day = datetime.strptime(day_dir.name, "%Y-%m-%d")
        except ValueError:
            continue
        if day < cutoff:
            break
        for path in day_dir.glob("*.md"):
            try:
                body = path.read_text(encoding="utf-8")
            except OSError:
                continue
            body_low = body.lower()
            if q and q not in body_low:
                continue
            if person and f"[[{person}" not in body and f"people: [{person}" not in body and f"{person}" not in body_low:
                continue
            if project and f"[[{project}" not in body and f"projects: [{project}" not in body:
                continue
            title = path.stem
            m = re.search(r"^# (.+)$", body, re.MULTILINE)
            if m:
                title = m.group(1).strip()
            excerpt_lines = [
                ln for ln in body.splitlines()
                if ln.strip() and not ln.startswith(("---", "#", "date:", "time:", "people:", "projects:"))
            ]
            excerpt = " ".join(excerpt_lines)[:300]
            hits.append((day_dir.name, f"{day_dir.name}/{path.stem}", f"**{title}** — {excerpt}"))
            if len(hits) >= 20:
                break
        if len(hits) >= 20:
            break

    if not hits:
        crit = []
        if q: crit.append(f"query='{q}'")
        if person: crit.append(f"person={person}")
        if project: crit.append(f"project={project}")
        crit.append(f"days={days}")
        return f"(no events — {', '.join(crit)})"

    return "\n\n".join(f"### {slug}\n{excerpt}" for _, slug, excerpt in hits)


def _goals_mutate(name: str, args: dict) -> str:
    """Route a mutation tool to deja.goals.apply_tasks_update."""
    _mcp_audit_context()
    from deja.goals import apply_tasks_update

    needle = args.get("needle", "")
    reason = args.get("reason", "")
    update: dict = {}

    if name == "add_task":
        update["add_tasks"] = [args.get("description", "")]
    elif name == "complete_task":
        update["complete_tasks"] = [needle]
    elif name == "archive_task":
        update["archive_tasks"] = [{"needle": needle, "reason": reason or "archived via MCP"}]
    elif name == "add_waiting_for":
        name_txt = args.get("person_name", "").strip()
        slug = args.get("person_slug", "").strip()
        what = args.get("what", "").strip()
        if slug and name_txt:
            formatted = f"**[[{slug}|{name_txt}]]** — {what}"
        elif name_txt:
            formatted = f"**{name_txt}** — {what}"
        else:
            return "(add_waiting_for requires person_name)"
        update["add_waiting"] = [formatted]
    elif name == "resolve_waiting_for":
        update["resolve_waiting"] = [needle]
    elif name == "archive_waiting_for":
        update["archive_waiting"] = [{"needle": needle, "reason": reason or "archived via MCP"}]
    elif name == "add_reminder":
        update["add_reminders"] = [{
            "date": args.get("date", ""),
            "question": args.get("question", ""),
            "topics": args.get("topics") or [],
        }]
    elif name == "resolve_reminder":
        update["resolve_reminders"] = [needle]
    elif name == "archive_reminder":
        update["archive_reminders"] = [{"needle": needle, "reason": reason or "archived via MCP"}]
    else:
        return f"(unknown mutation: {name})"

    changes = apply_tasks_update(update)

    try:
        from deja.wiki_git import commit_changes
        commit_changes(f"hermes: {name} — {reason or '(no reason)'}")
    except Exception:
        pass

    return f"ok — applied {changes} change(s) via {name}"


def _execute_action(action_type: str, params: dict, reason: str) -> str:
    """Route an action (email draft, calendar, task) through goal_actions."""
    _mcp_audit_context()
    from deja.goal_actions import execute_action
    success = execute_action({"type": action_type, "params": params, "reason": reason})
    return f"{'ok' if success else 'failed'} — {action_type}"


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

    if name == "daily_briefing":
        return _daily_briefing()

    if name == "search_deja":
        return _search_deja(args.get("query", ""), args.get("limit", 10))

    if name == "get_page":
        return _get_page(args.get("category", ""), args.get("slug", ""))

    if name == "list_goals":
        return _list_goals()

    if name == "search_events":
        return _search_events(
            query=args.get("query", ""),
            days=args.get("days", 7),
            person=args.get("person"),
            project=args.get("project"),
        )

    # Goal mutators — all route through deja.goals.apply_tasks_update,
    # which handles audit.record internally. We set the trigger context
    # so the entry shows trigger.kind=mcp / detail=hermes.
    if name in (
        "add_task", "complete_task", "archive_task",
        "add_waiting_for", "resolve_waiting_for", "archive_waiting_for",
        "add_reminder", "resolve_reminder", "archive_reminder",
    ):
        return _goals_mutate(name, args)

    if name == "execute_action":
        return _execute_action(
            action_type=args.get("type", ""),
            params=args.get("params") or {},
            reason=args.get("reason", ""),
        )

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
