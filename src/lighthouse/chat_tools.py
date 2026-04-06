"""Free-reign wiki tool surface for the chat agent.

When the user types a structural request into the notch chat ("delete the
terafab page", "rename coach-rob-robert-toy to robert-toy", "merge
tom-peffer into tom-thurlow"), the chat endpoint hands Pro a set of wiki
tools it can call directly. Pro plans the edits, executes them as tool
calls, and narrates what it did in the stream — no Flash-Lite integration
cycle in the loop, no structured `wiki_updates` JSON to squeeze through.

Tool surface:
  - ``list_pages(category)`` — discover what exists
  - ``read_page(category, slug)`` — inspect current content before editing
  - ``write_page(category, slug, content, reason)`` — create or overwrite
  - ``delete_page(category, slug, reason)`` — remove
  - ``rename_page(category, old_slug, new_slug, reason)`` — atomic rename

Every mutating tool requires a ``reason`` argument that gets logged to
``log.md`` and becomes part of the git commit message. Every path is
validated to stay inside ``WIKI_DIR``. After each successful mutation the
wiki auto-commits, so per-tool-call reversibility is one ``git revert``.

The tools are deliberately thin. They don't try to be smart about merges
or cross-page link updates — that's Pro's job via the LLM. Pro reads the
pages it needs, plans the changes, and emits the tool calls in sequence.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from google.genai import types

from lighthouse.config import WIKI_DIR
from lighthouse import wiki as wiki_store
from lighthouse.activity_log import append_log_entry

log = logging.getLogger(__name__)


CATEGORIES = ("people", "projects", "events")
# Event slugs can include a date prefix: "2026-04-05/event-name"
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*(?:/[a-z0-9][a-z0-9-]*)?$")


@dataclass
class ToolResult:
    """Return value of any tool call — always JSON-serializable.

    ``ok`` is the primary success flag. ``message`` is a short
    human-readable summary (also sent to the LLM as the function
    response, so keep it concise). ``data`` carries structured payload
    for read operations.
    """
    ok: bool
    message: str
    data: dict | None = None

    def as_response_dict(self) -> dict:
        """Shape sent back to the model as the function response body."""
        out: dict = {"ok": self.ok, "message": self.message}
        if self.data is not None:
            out["data"] = self.data
        return out


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _validate_category(category: str) -> str | None:
    if category not in CATEGORIES:
        return f"category must be 'people' or 'projects', got {category!r}"
    return None


def _validate_slug(slug: str) -> str | None:
    if not slug or not isinstance(slug, str):
        return "slug is required"
    if not _SLUG_RE.match(slug):
        return f"slug must be kebab-case ([a-z0-9-]+), got {slug!r}"
    if ".." in slug or "/" in slug or "\\" in slug:
        return f"slug must not contain path separators or ..: {slug!r}"
    return None


def _page_path(category: str, slug: str) -> Path:
    """Resolve a category+slug to its on-disk path, guaranteed inside WIKI_DIR."""
    p = (WIKI_DIR / category / f"{slug}.md").resolve()
    wiki_root = WIKI_DIR.resolve()
    if wiki_root not in p.parents:
        raise ValueError(f"resolved path {p} escapes wiki root {wiki_root}")
    return p


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def list_pages(category: str | None = None) -> ToolResult:
    """Return every wiki page's slug and title, optionally filtered by category."""
    cats = CATEGORIES if category is None else (category,)
    if category is not None and _validate_category(category):
        return ToolResult(ok=False, message=_validate_category(category) or "")

    pages: list[dict] = []
    for cat in cats:
        cat_dir = WIKI_DIR / cat
        if not cat_dir.is_dir():
            continue
        for path in sorted(cat_dir.glob("*.md")):
            if path.name.startswith((".", "_")):
                continue
            title = path.stem.replace("-", " ").title()
            try:
                for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                    s = line.strip()
                    if s.startswith("# "):
                        title = s[2:].strip()
                        break
            except OSError:
                pass
            pages.append({"category": cat, "slug": path.stem, "title": title})

    return ToolResult(
        ok=True,
        message=f"{len(pages)} page(s)",
        data={"pages": pages},
    )


def read_page(category: str, slug: str) -> ToolResult:
    err = _validate_category(category) or _validate_slug(slug)
    if err:
        return ToolResult(ok=False, message=err)
    path = _page_path(category, slug)
    if not path.exists():
        return ToolResult(
            ok=True,
            message=f"{category}/{slug} does not exist",
            data={"exists": False, "content": ""},
        )
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return ToolResult(ok=False, message=f"read failed: {e}")
    return ToolResult(
        ok=True,
        message=f"{category}/{slug} — {len(content)} chars",
        data={"exists": True, "content": content},
    )


def write_page(category: str, slug: str, content: str, reason: str) -> ToolResult:
    """Create or overwrite a wiki page. Backs up any prior version."""
    err = _validate_category(category) or _validate_slug(slug)
    if err:
        return ToolResult(ok=False, message=err)
    if not content or not content.strip():
        return ToolResult(ok=False, message="content is required and must not be empty")
    if not reason or not reason.strip():
        return ToolResult(ok=False, message="reason is required (say why the change is being made)")

    path = _page_path(category, slug)
    was_new = not path.exists()
    try:
        wiki_store.write_page(category, slug, content)
    except Exception as e:
        log.exception("write_page tool failed for %s/%s", category, slug)
        return ToolResult(ok=False, message=f"write failed: {e}")

    action = "created" if was_new else "updated"
    append_log_entry("chat", f"{action} {category}/{slug} — {reason[:120]}")
    log.info("chat tool write_page: %s %s/%s — %s", action, category, slug, reason[:100])
    return ToolResult(
        ok=True,
        message=f"{action} {category}/{slug}",
        data={"was_new": was_new},
    )


def delete_page(category: str, slug: str, reason: str) -> ToolResult:
    err = _validate_category(category) or _validate_slug(slug)
    if err:
        return ToolResult(ok=False, message=err)
    if not reason or not reason.strip():
        return ToolResult(ok=False, message="reason is required (say why the page is being removed)")

    try:
        ok = wiki_store.delete_page(category, slug)
    except Exception as e:
        log.exception("delete_page tool failed for %s/%s", category, slug)
        return ToolResult(ok=False, message=f"delete failed: {e}")

    if not ok:
        return ToolResult(ok=True, message=f"{category}/{slug} did not exist (no-op)")

    append_log_entry("chat", f"deleted {category}/{slug} — {reason[:120]}")
    log.info("chat tool delete_page: %s/%s — %s", category, slug, reason[:100])
    return ToolResult(ok=True, message=f"deleted {category}/{slug}")


def rename_page(category: str, old_slug: str, new_slug: str, reason: str) -> ToolResult:
    """Atomic rename: read old content, write to new slug, delete old.

    Doesn't attempt to update inbound ``[[old-slug]]`` references on other
    pages — the linkify pass and the reflect cycle will normalize those.
    If an inbound ref becomes broken, it'll show up in the next
    ``find_broken_refs`` pass.
    """
    err = (
        _validate_category(category)
        or _validate_slug(old_slug)
        or _validate_slug(new_slug)
    )
    if err:
        return ToolResult(ok=False, message=err)
    if old_slug == new_slug:
        return ToolResult(ok=False, message="old_slug and new_slug are identical")
    if not reason or not reason.strip():
        return ToolResult(ok=False, message="reason is required")

    old_path = _page_path(category, old_slug)
    new_path = _page_path(category, new_slug)

    if not old_path.exists():
        return ToolResult(ok=False, message=f"source page {category}/{old_slug} does not exist")
    if new_path.exists():
        return ToolResult(
            ok=False,
            message=f"target {category}/{new_slug} already exists — use write_page + delete_page if you mean to merge",
        )

    try:
        content = old_path.read_text(encoding="utf-8", errors="replace")
        wiki_store.write_page(category, new_slug, content)
        wiki_store.delete_page(category, old_slug)
    except Exception as e:
        log.exception("rename_page tool failed for %s/%s → %s", category, old_slug, new_slug)
        return ToolResult(ok=False, message=f"rename failed: {e}")

    append_log_entry(
        "chat",
        f"renamed {category}/{old_slug} → {category}/{new_slug} — {reason[:120]}",
    )
    log.info("chat tool rename_page: %s/%s → %s/%s — %s",
             category, old_slug, category, new_slug, reason[:100])
    return ToolResult(
        ok=True,
        message=f"renamed {category}/{old_slug} → {category}/{new_slug}",
    )


# ---------------------------------------------------------------------------
# Dispatch + SDK bindings
# ---------------------------------------------------------------------------

_TOOLS = {
    "list_pages": list_pages,
    "read_page": read_page,
    "write_page": write_page,
    "delete_page": delete_page,
    "rename_page": rename_page,
}


def execute_tool_call(name: str, args: dict) -> ToolResult:
    """Route a tool-call name + args dict to the right Python function.

    Unknown names return an error result rather than raising, so the LLM
    can see the error message and recover. Argument type errors likewise
    surface as results, not exceptions.
    """
    fn = _TOOLS.get(name)
    if fn is None:
        return ToolResult(ok=False, message=f"unknown tool {name!r}")
    try:
        return fn(**(args or {}))
    except TypeError as e:
        return ToolResult(ok=False, message=f"bad arguments to {name}: {e}")
    except Exception as e:
        log.exception("tool %s blew up", name)
        return ToolResult(ok=False, message=f"{name} failed: {e}")


def build_tool_declarations() -> list[types.Tool]:
    """Return the ``google-genai`` Tool list to pass to ``generate_content``.

    Schemas are intentionally minimal — the parameter docstrings and type
    hints are enough for Pro to call correctly. Each mutating tool
    documents its ``reason`` parameter to push the model toward
    meaningful audit trails instead of empty strings.
    """
    decls = [
        types.FunctionDeclaration(
            name="list_pages",
            description=(
                "List every wiki page with its category, slug, and title. "
                "Optionally filter by category ('people' or 'projects'). "
                "Call this first when you need to find pages matching a description."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "enum": ["people", "projects"],
                        "description": "Optional filter — omit to list both categories.",
                    },
                },
            },
        ),
        types.FunctionDeclaration(
            name="read_page",
            description=(
                "Read the current full markdown content of one wiki page, "
                "including YAML frontmatter. Use this before rewriting so you "
                "preserve existing frontmatter and don't drop information."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "category": {"type": "string", "enum": ["people", "projects"]},
                    "slug": {"type": "string", "description": "kebab-case identifier"},
                },
                "required": ["category", "slug"],
            },
        ),
        types.FunctionDeclaration(
            name="write_page",
            description=(
                "Create or overwrite a wiki page with new markdown content. "
                "Include YAML frontmatter at the top when appropriate. Always "
                "read_page first if the page might exist, so you can preserve "
                "fields you don't mean to change."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "category": {"type": "string", "enum": ["people", "projects"]},
                    "slug": {"type": "string", "description": "kebab-case identifier"},
                    "content": {"type": "string", "description": "full markdown body including any frontmatter"},
                    "reason": {
                        "type": "string",
                        "description": "one sentence explaining why this change is being made; quoted in the git commit and activity log",
                    },
                },
                "required": ["category", "slug", "content", "reason"],
            },
        ),
        types.FunctionDeclaration(
            name="delete_page",
            description=(
                "Remove a wiki page. Backed up via git auto-commit so this "
                "is reversible via git revert. Only delete pages the user has "
                "clearly asked to remove or that are clearly invalid."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "category": {"type": "string", "enum": ["people", "projects"]},
                    "slug": {"type": "string"},
                    "reason": {
                        "type": "string",
                        "description": "one sentence — quote the user or explain the deletion rationale",
                    },
                },
                "required": ["category", "slug", "reason"],
            },
        ),
        types.FunctionDeclaration(
            name="rename_page",
            description=(
                "Atomically rename a page from old_slug to new_slug within "
                "the same category. Content is preserved verbatim. Inbound "
                "[[old_slug]] references on other pages are NOT updated by "
                "this tool — the daily reflect pass normalizes them."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "category": {"type": "string", "enum": ["people", "projects"]},
                    "old_slug": {"type": "string"},
                    "new_slug": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["category", "old_slug", "new_slug", "reason"],
            },
        ),
    ]
    return [types.Tool(function_declarations=decls)]
