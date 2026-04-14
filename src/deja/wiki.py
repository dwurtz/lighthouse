"""Personal wiki — entity-keyed knowledge pages + event log.

The wiki IS the agent's memory. Three categories:
  - ``people/`` — one page per real person
  - ``projects/`` — one page per active project, goal, or life thread
  - ``events/YYYY-MM-DD/`` — timestamped event pages, linked from entities

Entity pages describe **state** (who/what something IS). Event pages
describe **what happened** (timestamped, linked to entities). Entity
pages reference events via ``[[event-slug]]``; events reference entities
via ``[[person-or-project-slug]]``. QMD indexes all three categories.

The wiki lives at ``~/Deja/`` so it's browsable in Finder and
openable as an Obsidian vault.
"""

from __future__ import annotations
from deja.config import WIKI_DIR

import logging
import re
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)


CATEGORIES = ("people", "projects", "events")


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "unnamed"


def ensure_dirs():
    for category in CATEGORIES:
        (WIKI_DIR / category).mkdir(parents=True, exist_ok=True)
    (WIKI_DIR / ".backups").mkdir(parents=True, exist_ok=True)
    # Events get date subdirectories created on write, not upfront


def read_all_pages() -> list[dict]:
    """Return every wiki page as {category, slug, title, content}."""
    if not WIKI_DIR.exists():
        return []
    pages: list[dict] = []
    for category in CATEGORIES:
        cat_dir = WIKI_DIR / category
        if not cat_dir.exists():
            continue
        for md in sorted(cat_dir.glob("*.md")):
            content = md.read_text()
            title = md.stem.replace("-", " ").title()
            m = re.match(r"^#\s+(.+)$", content, re.MULTILINE)
            if m:
                title = m.group(1).strip()
            pages.append({
                "category": category,
                "slug": md.stem,
                "title": title,
                "content": content,
            })
    return pages


def backup_page(path: Path):
    if not path.exists():
        return
    backup_dir = WIKI_DIR / ".backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = backup_dir / f"{path.parent.name}-{path.stem}-{ts}.md"
    backup.write_text(path.read_text())


# Matches a YAML frontmatter block at the very start of a page:
#     ---
#     aliases: [...]
#     ---
# Used to extract/preserve metadata added by the reflect
# pass. The 5-minute cycle (Flash Lite) sometimes strips this on
# rewrites even when instructed to preserve it, so we enforce it here
# in deterministic code rather than relying on LLM discipline.
_FRONTMATTER_RE = re.compile(r"\A(---\s*\n.*?\n---\s*\n)", re.DOTALL)

# Catches the one-line corruption pattern integrate sometimes produces:
#     ---date: 2026-04-06time: "17:47"people: [foo]projects: [bar]---
# (no newlines between keys). 30+ event files currently have this shape.
_ONELINE_FRONTMATTER_RE = re.compile(r"\A---([^\n]+?)---\s*\n?", re.DOTALL)


def extract_frontmatter(content: str) -> tuple[str, str]:
    """Split `content` into (frontmatter_block, body).

    Returns ("", content) if no frontmatter is present. The frontmatter
    block includes its trailing `---\\n` so grafting it back is just a
    string concatenation. Does NOT match one-line-corrupted frontmatter
    (use ``canonicalize_frontmatter`` to repair those first).
    """
    m = _FRONTMATTER_RE.match(content)
    if not m:
        return "", content
    return m.group(1), content[m.end():]


def _split_inline_yaml(inline: str) -> list[tuple[str, str]]:
    """Split a one-line YAML blob into (key, value) pairs.

    Input looks like ``date: 2026-04-06time: "17:47"people: [foo]projects: [bar]``.
    Returns a list of tuples preserving order. Quoted strings and
    bracketed lists are kept intact. Used by canonicalize_frontmatter
    to repair integrate's one-line corruption.
    """
    pairs: list[tuple[str, str]] = []
    # Keys are always a-z_ followed by colon. Walk the string using a
    # regex that captures everything up to the next key or end.
    key_re = re.compile(r"([a-z_][a-z0-9_]*)\s*:\s*", re.IGNORECASE)
    matches = list(key_re.finditer(inline))
    for i, m in enumerate(matches):
        key = m.group(1)
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(inline)
        value = inline[start:end].rstrip()
        # Trim trailing whitespace but preserve leading brackets/quotes
        pairs.append((key, value.strip()))
    return pairs


def canonicalize_frontmatter(content: str) -> tuple[str, bool]:
    """Repair one-line corrupted frontmatter to clean multi-line form.

    Returns (maybe_repaired_content, was_repaired). If the content has
    clean multi-line frontmatter (or no frontmatter at all), returns
    it unchanged with ``was_repaired=False``.

    This fixes the shape integrate occasionally produces where all
    keys land on one line between the opening and closing ``---``:

        ---date: 2026-04-06time: "17:47"people: [foo]projects: [bar]---

    becomes:

        ---
        date: 2026-04-06
        time: "17:47"
        people: [foo]
        projects: [bar]
        ---
    """
    # Already canonical? Done.
    if _FRONTMATTER_RE.match(content):
        return content, False
    m = _ONELINE_FRONTMATTER_RE.match(content)
    if not m:
        return content, False
    inline = m.group(1).strip()
    pairs = _split_inline_yaml(inline)
    if not pairs:
        return content, False
    new_fm = "---\n" + "\n".join(f"{k}: {v}" for k, v in pairs) + "\n---\n"
    body = content[m.end():].lstrip("\n")
    return new_fm + body, True


def preserve_frontmatter(new_content: str, old_content: str) -> tuple[str, bool]:
    """If the old version had frontmatter and the new one doesn't, graft
    the old frontmatter back on. Returns (maybe_repaired, was_grafted).

    This is the deterministic safety net that makes the nightly pass's
    retrieval metadata stable across 5-minute cycle rewrites. Flash Lite
    has shown it will strip frontmatter during page rewrites even when
    explicitly told not to; waiting on prompt-level reliability isn't
    an option given we run a cycle every few minutes. Pure code guard.

    Rules:
      • Old has FM, new has FM           → keep new (the LLM updated it)
      • Old has FM, new has no FM        → graft old FM onto new body
      • Old has no FM, new has FM        → keep new (new FM being added)
      • Neither has FM                   → nothing to do
    """
    old_fm, _ = extract_frontmatter(old_content)
    new_fm, new_body = extract_frontmatter(new_content)
    if old_fm and not new_fm:
        return old_fm + new_body.lstrip("\n"), True
    return new_content, False


def delete_page(category: str, slug: str) -> bool:
    """Delete a wiki page or event. Backs it up to ``.backups`` first.

    Returns ``True`` if a page was removed, ``False`` if the page did
    not exist (no-op — still safe to call). Raises ``ValueError`` for
    an unknown category. For events, the slug may include a date prefix.

    Shared by the 5-minute integrate cycle and the nightly reflect pass
    so both take the same code path (backup → unlink → log).
    """
    if category not in CATEGORIES:
        raise ValueError(f"bad category: {category}")
    path = _resolve_page_path(category, slug)
    if not path.exists():
        return False
    backup_page(path)
    path.unlink()
    log.info("wiki: deleted %s/%s", category, slug)
    return True


def _resolve_page_path(category: str, slug: str) -> Path:
    """Resolve a category + slug to a filesystem path.

    For ``people`` and ``projects``, the path is simply
    ``WIKI_DIR/<category>/<slug>.md``.

    For ``events``, the slug MAY include a date prefix:
    ``2026-04-05/amanda-shared-sales-data`` → resolves to
    ``WIKI_DIR/events/2026-04-05/amanda-shared-sales-data.md``.
    If no date prefix, today's date is used as the subdirectory.
    """
    if category == "events":
        if "/" in slug:
            # slug = "2026-04-05/amanda-shared-sales-data"
            date_part, event_slug = slug.split("/", 1)
            return WIKI_DIR / "events" / date_part / f"{slugify(event_slug)}.md"
        else:
            # No date → use today
            today = datetime.now().strftime("%Y-%m-%d")
            return WIKI_DIR / "events" / today / f"{slugify(slug)}.md"
    return WIKI_DIR / category / f"{slugify(slug)}.md"


def write_page(category: str, slug: str, content: str) -> Path:
    """Write (or overwrite) a wiki page or event. Backs up the old version first.

    For ``events``, the slug can include a date prefix
    (``2026-04-05/event-name``). If no prefix, today's date is used.
    Parent directories are created automatically.

    If the old version had YAML frontmatter and the new content omitted
    it, the old frontmatter is grafted back onto the new body — a
    deterministic guard against LLM rewrites stripping retrieval
    metadata the reflect pass put there.
    """
    if category not in CATEGORIES:
        raise ValueError(f"bad category: {category}")
    ensure_dirs()
    path = _resolve_page_path(category, slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        old_content = path.read_text()
        backup_page(path)
        content, grafted = preserve_frontmatter(content, old_content)
        if grafted:
            log.info(
                "wiki: grafted frontmatter back onto %s/%s (LLM stripped it)",
                category, slug,
            )

    # Auto-repair one-line frontmatter corruption before persisting.
    # Without this, integrate's occasional single-line YAML output
    # ("---date: ...time: ...people: [...]---" on one line) propagates
    # through retrieval, cluster analysis, and display. The check is
    # cheap and deterministic; repair is safer than relying on
    # prompt-level discipline to always produce multi-line YAML.
    content, canonicalized = canonicalize_frontmatter(content)
    if canonicalized:
        log.warning(
            "wiki: canonicalized one-line frontmatter on %s/%s "
            "(integrate produced the broken form — consider tightening "
            "the prompt if this becomes frequent)",
            category, slug,
        )

    path.write_text(content.rstrip() + "\n")
    return path


def render_for_prompt(pages: list[dict] | None = None) -> str:
    """Format the full wiki as text suitable for injection into an LLM prompt."""
    if pages is None:
        pages = read_all_pages()
    if not pages:
        return "(no wiki pages yet)"
    chunks = []
    for p in pages:
        chunks.append(
            f"### {p['category']}/{p['slug']}  —  {p['title']}\n{p['content'].strip()}"
        )
    return "\n\n".join(chunks)


def apply_updates(updates: list[dict]) -> int:
    """Apply wiki updates returned by the unified analysis call.

    Each update is {category, slug, action, content, reason}. Writes each page,
    rebuilds the index, refreshes the QMD search index, and commits the
    changes to git. Returns the number of pages successfully written.
    """
    if not updates:
        return 0

    ensure_dirs()

    # Make sure the wiki is a git repo before we write anything
    try:
        from deja.wiki_git import ensure_repo
        ensure_repo()
    except Exception:
        pass

    applied = 0
    changed_slugs: list[str] = []
    for upd in updates:
        action = (upd.get("action") or "update").lower()
        category = upd.get("category")
        slug = upd.get("slug", "")
        reason = (upd.get("reason") or "").strip()

        if category not in CATEGORIES or not slug:
            log.warning("Skipping invalid wiki update: %s", upd)
            continue

        if action == "delete":
            try:
                removed = delete_page(category, slug)
            except Exception:
                log.exception("Failed to delete wiki page %s/%s", category, slug)
                continue
            if removed:
                applied += 1
                changed_slugs.append(f"{category}/{slug} (deleted)")
                from deja import audit
                audit.record(
                    "wiki_delete",
                    target=f"{category}/{slug}",
                    reason=reason or "(no reason given)",
                )
            else:
                log.info(
                    "Wiki delete no-op: %s/%s does not exist", category, slug
                )
            continue

        content = upd.get("content", "")
        if not content:
            log.warning("Skipping invalid wiki update (no content): %s", upd)
            continue
        try:
            write_page(category, slug, content)
            applied += 1
            changed_slugs.append(f"{category}/{slug}")
            log.info("Wiki %s: %s/%s — %s",
                     action, category, slug, reason[:80])
            from deja import audit
            audit.record(
                "event_create" if category == "events" else "wiki_write",
                target=f"{category}/{slug}",
                reason=reason or "(no reason given)",
            )
        except Exception:
            log.exception("Failed to write wiki page %s/%s", category, slug)

    if applied > 0:
        # Rebuild the top-level index.md so the next cycle's prompt sees it
        try:
            from deja.wiki_catalog import rebuild_index
            rebuild_index()
        except Exception:
            log.debug("wiki_index rebuild failed", exc_info=True)

        # Refresh QMD so the next integrate cycle's retrieval + the MCP
        # Context Engine see fresh content. `qmd update` re-indexes changed
        # files (including new event pages). We skip `qmd embed` here
        # (it's slower) — dedup (src/deja/dedup.py) runs `qmd update &&
        # qmd embed` at the start of every 3×/day pass, which is the
        # scheduled refresh point for the vector index.
        try:
            from deja.llm.search import refresh_index
            refresh_index()
        except Exception:
            log.debug("QMD refresh after wiki update failed", exc_info=True)
        try:
            import subprocess
            subprocess.run(["qmd", "update"], capture_output=True, timeout=15)
        except Exception:
            log.debug("QMD update after wiki update failed", exc_info=True)

        # Commit to git — free version history for every wiki change
        try:
            from deja.wiki_git import commit_changes
            message = "cycle: " + ", ".join(changed_slugs[:5])
            if len(changed_slugs) > 5:
                message += f" (+{len(changed_slugs) - 5} more)"
            commit_changes(message)
        except Exception:
            log.debug("wiki_git commit failed", exc_info=True)

    return applied
