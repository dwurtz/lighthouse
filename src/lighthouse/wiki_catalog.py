"""Wiki index generator for Lighthouse.

Maintains a categorized `index.md` catalog at the wiki root that lists every
page under `people/` and `projects/` with a one-line summary. The LLM reads
this index first during analysis cycles to decide which pages are relevant,
then drills into specific pages.

Two categories only: people and projects. Everything ongoing — goals,
initiatives, life threads, situations — is a project.
"""

from __future__ import annotations
from lighthouse.config import WIKI_DIR

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)


INDEX_PATH = WIKI_DIR / "index.md"
CATEGORIES = ("people", "projects")

_H1_RE = re.compile(r"^#\s+(.+?)\s*$")
_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
_SUMMARY_MAX = 140

# Hard cap on entries written to index.md. Beyond this, the prompt cost of
# shipping the full catalog to every cycle + every vision call outweighs the
# grounding benefit. Pages dropped from the index still exist on disk and are
# still retrievable via QMD — they're just not in the at-a-glance catalog.
_MAX_ENTRIES = 200

# Substrings that indicate a summary is a placeholder, not real content. We
# skip these from the index entirely — they waste tokens and give the vision
# model nothing to ground on.
_PLACEHOLDER_SUMMARIES = {"---", "--", "tbd", "todo", "placeholder", ""}


def _strip_frontmatter(lines: list[str]) -> list[str]:
    """Drop a leading YAML frontmatter block (``---\\n...\\n---``) if present.

    Obsidian-style pages start with a fenced YAML block for tags, aliases,
    keywords, etc. The summary scan must skip over it — otherwise the
    opening ``---`` delimiter gets picked up as the first non-heading line
    and the page looks like a placeholder stub."""
    if not lines or lines[0].strip() != "---":
        return lines
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return lines[i + 1:]
    # Unterminated frontmatter — treat the whole file as content to be safe
    return lines


def _extract_title_and_summary(path: Path) -> tuple[str, str]:
    """Return (title, summary) for a single markdown page."""
    title = ""
    summary = ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        logger.warning("wiki_index: failed to read %s: %s", path, e)
        return (path.stem.replace("-", " ").replace("_", " ").title(), "")

    lines = _strip_frontmatter(text.splitlines())

    for raw_line in lines:
        line = raw_line.strip()
        if not title:
            m = _H1_RE.match(line)
            if m:
                title = m.group(1).strip()
                continue
        if not summary and line and not line.startswith("#"):
            cleaned = _WIKILINK_RE.sub(lambda m: m.group(1), line)
            cleaned = cleaned.strip()
            if cleaned:
                if len(cleaned) > _SUMMARY_MAX:
                    cleaned = cleaned[: _SUMMARY_MAX - 3].rstrip() + "..."
                summary = cleaned
        if title and summary:
            break

    if not title:
        title = path.stem.replace("-", " ").replace("_", " ").title()
    return title, summary


def _is_placeholder_summary(summary: str) -> bool:
    """Is this summary line a stub the LLM hasn't filled in yet?"""
    if not summary:
        return True
    return summary.strip().lower() in _PLACEHOLDER_SUMMARIES


def _collect_category(category: str) -> list[tuple[str, str, str, float]]:
    """Return (slug, title, summary, mtime) tuples for one category.

    Placeholder summaries (`---`, `TBD`, empty) are normalized to an empty
    string so the renderer emits a bare ``- [[slug]]`` line. The slug itself
    is still grounding signal for the LLM (it teaches the model which wiki
    entities exist), we just drop the noisy summary text. The mtime is kept
    so the caller can apply a budget across categories.
    """
    cat_dir = WIKI_DIR / category
    if not cat_dir.is_dir():
        return []

    entries: list[tuple[str, str, str, float]] = []
    try:
        for path in cat_dir.iterdir():
            if not path.is_file():
                continue
            if path.suffix.lower() != ".md":
                continue
            name = path.name
            if name.startswith(".") or name.startswith("_"):
                continue
            slug = path.stem
            title, summary = _extract_title_and_summary(path)
            if _is_placeholder_summary(summary):
                summary = ""  # bare slug, no noisy dashes
            try:
                mtime = path.stat().st_mtime
            except OSError:
                mtime = 0.0
            entries.append((slug, title, summary, mtime))
    except OSError as e:
        logger.warning("wiki_index: failed to scan %s: %s", cat_dir, e)
        return []

    entries.sort(key=lambda t: t[0].lower())
    return entries


_HEADER = (
    "# Wiki Index\n"
    "\n"
    "*Auto-generated catalog of every page. The LLM reads this first to "
    "decide what's relevant, then drills into specific pages. Do not edit "
    "by hand — rebuilt on every wiki change.*\n"
)

_CATEGORY_TITLES = {
    "people": "People",
    "projects": "Projects",
}


def rebuild_index() -> int:
    """Scan every wiki page and rewrite index.md.

    Walks `WIKI_DIR/{people,projects}/*.md`, extracts the title (first H1
    line) and a one-line summary (first non-heading sentence, truncated).
    Writes a categorized index to `index.md` at the wiki root.

    Returns the number of pages indexed. Swallows errors and logs them —
    never raises. Safe to call on every wiki change.
    """
    try:
        if not WIKI_DIR.is_dir():
            logger.warning("wiki_index: wiki dir does not exist: %s", WIKI_DIR)
            return 0

        per_category: dict[str, list[tuple[str, str, str, float]]] = {}
        total = 0
        for category in CATEGORIES:
            entries = _collect_category(category)
            per_category[category] = entries
            total += len(entries)

        # Apply the hard cap across categories. When we're over budget, keep
        # the most-recently-touched pages — those are the ones David is
        # actively thinking about. Everything else still exists on disk and
        # is retrievable via QMD, it's just not in the at-a-glance catalog.
        if total > _MAX_ENTRIES:
            flat = [
                (category, slug, title, summary, mtime)
                for category in CATEGORIES
                for (slug, title, summary, mtime) in per_category[category]
            ]
            flat.sort(key=lambda t: t[4], reverse=True)
            kept = flat[:_MAX_ENTRIES]
            dropped = total - len(kept)
            logger.info(
                "wiki_index: %d pages exceeds cap %d — keeping %d most-recent, dropping %d from index",
                total, _MAX_ENTRIES, len(kept), dropped,
            )
            per_category = {cat: [] for cat in CATEGORIES}
            for category, slug, title, summary, mtime in kept:
                per_category[category].append((slug, title, summary, mtime))
            for cat in CATEGORIES:
                per_category[cat].sort(key=lambda t: t[0].lower())
            total = len(kept)

        if total == 0:
            placeholder = _HEADER + "\n*No pages yet.*\n"
            try:
                INDEX_PATH.write_text(placeholder, encoding="utf-8")
            except OSError as e:
                logger.warning("wiki_index: failed to write placeholder index: %s", e)
            return 0

        parts: list[str] = [_HEADER]
        for category in CATEGORIES:
            entries = per_category[category]
            if not entries:
                continue
            parts.append(f"\n## {_CATEGORY_TITLES[category]}\n\n")
            for slug, _title, summary, _mtime in entries:
                if summary:
                    parts.append(f"- [[{slug}]] — {summary}\n")
                else:
                    parts.append(f"- [[{slug}]]\n")

        content = "".join(parts)
        try:
            INDEX_PATH.write_text(content, encoding="utf-8")
        except OSError as e:
            logger.warning("wiki_index: failed to write index: %s", e)
            return 0

        return total
    except Exception as e:  # noqa: BLE001 - never raise, log and return
        logger.warning("wiki_index: unexpected error during rebuild: %s", e)
        return 0


def render_index_for_prompt() -> str:
    """Return the current index.md content for LLM prompt injection.

    Calls rebuild_index() first to ensure it's fresh. Returns empty string
    if there are no pages.
    """
    try:
        count = rebuild_index()
        if count == 0:
            return ""
        return INDEX_PATH.read_text(encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        logger.warning("wiki_index: failed to render index: %s", e)
        return ""
