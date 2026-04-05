"""Personal wiki — entity-keyed knowledge pages.

The wiki IS the agent's memory. The analysis cycle reads the wiki as context,
observes new signals, and rewrites affected pages in one LLM call. No separate
fact/commitment extraction step.

Wiki pages live in ~/Lighthouse/ so they're browsable in Finder
and openable as an Obsidian vault. Two categories only: people and projects.
Everything ongoing — goals, initiatives, life threads, situations — is a project.
"""

from __future__ import annotations
from lighthouse.config import WIKI_DIR

import logging
import re
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)


CATEGORIES = ("people", "projects")


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "unnamed"


def ensure_dirs():
    for category in CATEGORIES:
        (WIKI_DIR / category).mkdir(parents=True, exist_ok=True)
    (WIKI_DIR / ".backups").mkdir(parents=True, exist_ok=True)


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


def extract_frontmatter(content: str) -> tuple[str, str]:
    """Split `content` into (frontmatter_block, body).

    Returns ("", content) if no frontmatter is present. The frontmatter
    block includes its trailing `---\\n` so grafting it back is just a
    string concatenation.
    """
    m = _FRONTMATTER_RE.match(content)
    if not m:
        return "", content
    return m.group(1), content[m.end():]


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
    """Delete a wiki page. Backs it up to ``.backups`` first.

    Returns ``True`` if a page was removed, ``False`` if the page did
    not exist (no-op — still safe to call). Raises ``ValueError`` for
    an unknown category.

    Shared by the 5-minute integrate cycle and the nightly reflect pass
    so both take the same code path (backup → unlink → log).
    """
    if category not in CATEGORIES:
        raise ValueError(f"bad category: {category}")
    path = WIKI_DIR / category / f"{slugify(slug)}.md"
    if not path.exists():
        return False
    backup_page(path)
    path.unlink()
    log.info("wiki: deleted %s/%s", category, slug)
    return True


def write_page(category: str, slug: str, content: str) -> Path:
    """Write (or overwrite) a wiki page. Backs up the old version first.

    If the old version had YAML frontmatter and the new content omitted
    it, the old frontmatter is grafted back onto the new body — a
    deterministic guard against LLM rewrites stripping retrieval
    metadata the reflect pass put there.
    """
    if category not in CATEGORIES:
        raise ValueError(f"bad category: {category}")
    ensure_dirs()
    path = WIKI_DIR / category / f"{slugify(slug)}.md"
    if path.exists():
        old_content = path.read_text()
        backup_page(path)
        content, grafted = preserve_frontmatter(content, old_content)
        if grafted:
            log.info(
                "wiki: grafted frontmatter back onto %s/%s (LLM stripped it)",
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
        from lighthouse.wiki_git import ensure_repo
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
                # Loud activity-log entry so the user can see integrate
                # deletes in log.md alongside reflect deletes.
                try:
                    from lighthouse.activity_log import append_log_entry
                    append_log_entry(
                        "integrate",
                        f"deleted {category}/{slug} because {reason or '(no reason given)'}",
                    )
                except Exception:
                    log.debug("activity_log append failed", exc_info=True)
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
        except Exception:
            log.exception("Failed to write wiki page %s/%s", category, slug)

    if applied > 0:
        # Rebuild the top-level index.md so the next cycle's prompt sees it
        try:
            from lighthouse.wiki_catalog import rebuild_index
            rebuild_index()
        except Exception:
            log.debug("wiki_index rebuild failed", exc_info=True)

        # Refresh QMD so chat retrieval sees fresh content
        try:
            from lighthouse.llm.search import refresh_index
            refresh_index()
        except Exception:
            log.debug("QMD refresh after wiki update failed", exc_info=True)

        # Commit to git — free version history for every wiki change
        try:
            from lighthouse.wiki_git import commit_changes
            message = "cycle: " + ", ".join(changed_slugs[:5])
            if len(changed_slugs) > 5:
                message += f" (+{len(changed_slugs) - 5} more)"
            commit_changes(message)
        except Exception:
            log.debug("wiki_git commit failed", exc_info=True)

    return applied
