"""Wiki index generator for Déjà.

Maintains a categorized `index.md` catalog at the wiki root that lists every
page under `people/` and `projects/` with a one-line summary. The LLM reads
this index first during analysis cycles to decide which pages are relevant,
then drills into specific pages.

Two categories only: people and projects. Everything ongoing — goals,
initiatives, life threads, situations — is a project.
"""

from __future__ import annotations
from deja.config import WIKI_DIR

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
    "*Auto-generated catalog of every page, ordered by most-recently-"
    "touched first. LLM consumers (vision prompt, triage prefilter, "
    "integrate retrieval) read this top-down and get attention-weighted "
    "relevance for free. Do not edit by hand — rebuilt on every wiki "
    "change. Browse by category in the ``people/`` and ``projects/`` "
    "folders if you want structure.*\n"
)


def rebuild_index() -> int:
    """Scan every wiki page and rewrite index.md as a flat recency list.

    Walks ``WIKI_DIR/{people,projects}/*.md``, extracts the title (first
    H1 line) and a one-line summary (first non-heading sentence), and
    writes a single flat list to ``index.md`` sorted by mtime descending
    with slug as an alphabetical tiebreak.

    There are no ``## People`` / ``## Projects`` sections. The power
    users of this file are LLMs (vision prompt, triage prefilter,
    integrate retrieval) and none of them parse the category headers —
    they all want the hottest entries at the top regardless of type.
    Humans who want to browse by category can use the ``people/`` and
    ``projects/`` folders directly in Obsidian.

    Returns the number of pages indexed. Swallows errors and logs them —
    never raises. Safe to call on every wiki change.
    """
    try:
        if not WIKI_DIR.is_dir():
            logger.warning("wiki_index: wiki dir does not exist: %s", WIKI_DIR)
            return 0

        # Flat collection across all categories. We still iterate
        # CATEGORIES for the directory walk (that's where _collect_category
        # pulls from), but the per-category grouping ends here — after
        # this list comp, `entries` is one flat (slug, title, summary,
        # mtime) list ready to sort globally.
        entries: list[tuple[str, str, str, float]] = [
            row
            for category in CATEGORIES
            for row in _collect_category(category)
        ]
        total = len(entries)

        # Apply the hard cap across categories. When over budget, keep
        # the most-recently-touched pages — those are the ones David is
        # actively thinking about. Everything else still exists on disk
        # and is retrievable via QMD, it's just not in the at-a-glance
        # catalog.
        if total > _MAX_ENTRIES:
            entries.sort(key=lambda t: t[3], reverse=True)
            dropped = total - _MAX_ENTRIES
            entries = entries[:_MAX_ENTRIES]
            logger.info(
                "wiki_index: %d pages exceeds cap %d — keeping %d most-recent, dropping %d from index",
                total, _MAX_ENTRIES, len(entries), dropped,
            )
            total = len(entries)

        # Final ordering: recency-descending globally, alphabetical
        # tiebreak for stable ordering when mtimes are identical (rare,
        # but happens on fresh bulk-created wikis).
        entries.sort(key=lambda t: (-t[3], t[0].lower()))

        if total == 0:
            placeholder = _HEADER + "\n*No pages yet.*\n"
            try:
                INDEX_PATH.write_text(placeholder, encoding="utf-8")
            except OSError as e:
                logger.warning("wiki_index: failed to write placeholder index: %s", e)
            return 0

        parts: list[str] = [_HEADER, "\n"]
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


def render_index_for_prompt(
    *,
    max_lines: int | None = None,
    rebuild: bool = True,
) -> str:
    """Return the current index.md content for LLM prompt injection.

    Used as the single catalog-lookup helper across Deja:
      - ``wiki_retriever.build_analysis_context`` — full index, rebuilt
        (the integrate cycle wants a fresh snapshot).
      - ``deja.llm.prefilter.triage_batch`` — full index, no rebuild
        (the analysis cycle already rebuilt upstream).
      - ``deja.vision_local._build_prompt`` — truncated to ``max_lines``,
        no rebuild (called every 6s; we don't want to scan all pages
        that often).

    Args:
        max_lines: If set, keep only the first N lines of index.md.
            Used by vision to cap FastVLM's prompt size while still
            exposing the most-relevant entries once reflect learns
            to sort by recency (Phase B).
        rebuild: If True, call ``rebuild_index()`` first to guarantee
            freshness. Defaults True for callers that didn't rebuild
            upstream; set False when the analysis cycle has already
            done it.

    Returns empty string if there are no pages or on any error.
    """
    try:
        if rebuild:
            count = rebuild_index()
            if count == 0:
                return ""
        elif not INDEX_PATH.exists():
            return ""

        text = INDEX_PATH.read_text(encoding="utf-8")
        if max_lines is not None:
            lines = text.splitlines()
            text = "\n".join(lines[:max_lines])
        return text
    except Exception as e:  # noqa: BLE001
        logger.warning("wiki_index: failed to render index: %s", e)
        return ""
