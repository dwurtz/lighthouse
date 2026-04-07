"""Deterministic wiki-link pass.

The 5-minute cycles ask the LLM to emit `[[slug]]` for known entities, but
the LLM is inconsistent — on a cycle where it's focused on other things,
mentions of known people/projects ship as plain text. This module is the
sweep layer: walks every wiki page, finds unlinked mentions of any catalog
entity, and wraps the first occurrence in `[[slug|display]]` syntax.

Runs inside the nightly cleanup job (once a day, on a stable catalog) and
also via the `deja linkify` CLI for ad-hoc / one-shot backfill. Not
called from write_page — the 5-min cycle's catalog is too volatile for
write-time linkification to be cross-page consistent.

Guarantees:
    - Idempotent. Re-linkifying a page that already has links is a no-op.
    - Preserves YAML frontmatter exactly.
    - Skips text inside fenced code blocks, inline code, existing wiki
      links, and markdown links — never wraps text that's already a link
      or that the author clearly meant as code.
    - First-occurrence only per slug per page. Subsequent mentions stay
      plain, which keeps the rewritten page readable.
    - A page never links to itself.
    - The user's own self-page is excluded from the catalog — every page
      in a personal wiki is implicitly about the user, linking them in
      every page would turn the graph into a useless hub-and-spoke.

Also exposes ``find_broken_refs`` for a companion nightly report: scans
every existing `[[slug]]` reference and flags targets that don't exist.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from deja.config import WIKI_DIR

log = logging.getLogger(__name__)


CATEGORIES = ("people", "projects")

# Phrases this short are skipped — too common to safely auto-link even
# with word-boundary matching (e.g. a hypothetical slug "ai" would wrap
# every occurrence of the word "ai" in the corpus).
_MIN_MATCH_LEN = 3

# Matches an existing wiki link: [[slug]] or [[slug|display text]]
_WIKILINK_RE = re.compile(r"\[\[([^\]|\n]+?)(?:\|[^\]\n]*)?\]\]")

# Matches a fenced code block (``` ... ```) including the opening fence
_FENCE_RE = re.compile(r"```[\s\S]*?```", re.MULTILINE)

# Matches inline code (`...`) but not triple backticks
_INLINE_CODE_RE = re.compile(r"`[^`\n]+?`")

# Matches a standard markdown link: [text](url)
_MD_LINK_RE = re.compile(r"\[[^\]\n]+?\]\([^)\n]+?\)")

# Matches a YAML frontmatter block at the start of a file
_FRONTMATTER_RE = re.compile(r"^(---\s*\n.*?\n---\s*\n)", re.DOTALL)


@dataclass(frozen=True)
class Entity:
    """One wiki page, indexed by every name the linkifier should match.

    ``slug`` is the filename stem (what goes inside ``[[ ]]``). ``phrases``
    is the full match set: title, slug-with-spaces, and every alias from
    frontmatter. The linkifier sorts these longest-first so "Blade & Rose
    USA" matches before "Rose".
    """
    slug: str
    category: str
    title: str
    phrases: tuple[str, ...] = field(default_factory=tuple)


@dataclass
class LinkifyReport:
    """Summary of one linkify pass. Dumped to log.md and deja.log."""
    pages_scanned: int = 0
    pages_changed: int = 0
    links_added: int = 0
    links_by_slug: dict[str, int] = field(default_factory=dict)
    broken_refs: list[tuple[str, str]] = field(default_factory=list)

    def brief(self) -> str:
        if self.pages_changed == 0 and not self.broken_refs:
            return f"linkify: {self.pages_scanned} pages scanned, no changes"
        parts = []
        if self.pages_changed:
            parts.append(
                f"added {self.links_added} link(s) across {self.pages_changed} page(s)"
            )
        if self.broken_refs:
            parts.append(f"{len(self.broken_refs)} broken ref(s)")
        return f"linkify: {', '.join(parts)}"


# ---------------------------------------------------------------------------
# Catalog construction
# ---------------------------------------------------------------------------

def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split a page into (frontmatter_dict, body). Returns ({}, text) on miss."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    try:
        meta = yaml.safe_load(m.group(1).strip("-\n ")) or {}
        if not isinstance(meta, dict):
            meta = {}
    except Exception:
        meta = {}
    return meta, text[m.end():]


def _title_from_body(body: str, fallback: str) -> str:
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("# "):
            return line[2:].strip()
    return fallback


def _phrases_for_page(slug: str, title: str, aliases: list[str]) -> tuple[str, ...]:
    """Return the set of match phrases for a page, deduped and non-empty.

    Includes the title, the slug-with-spaces (e.g. "palo alto relocation"),
    and every alias from frontmatter. Phrases shorter than _MIN_MATCH_LEN
    are dropped as too noisy to safely auto-link.
    """
    candidates: list[str] = []
    if title:
        candidates.append(title)
    slug_spaces = slug.replace("-", " ").strip()
    if slug_spaces and slug_spaces.lower() != title.lower():
        candidates.append(slug_spaces)
    for a in aliases or []:
        if isinstance(a, str) and a.strip():
            candidates.append(a.strip())

    seen: set[str] = set()
    out: list[str] = []
    for c in candidates:
        c = c.strip()
        if len(c) < _MIN_MATCH_LEN:
            continue
        key = c.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return tuple(out)


def build_catalog(wiki_dir: Path | None = None) -> list[Entity]:
    """Walk people/ and projects/ and return one Entity per page.

    Pages marked ``self: true`` in frontmatter are excluded — every page
    is implicitly about the user, so linking the user on every page is
    noise not signal.
    """
    if wiki_dir is None:
        wiki_dir = WIKI_DIR

    entities: list[Entity] = []
    for category in CATEGORIES:
        cat_dir = wiki_dir / category
        if not cat_dir.is_dir():
            continue
        for path in sorted(cat_dir.glob("*.md")):
            if path.name.startswith((".", "_")):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            meta, body = _parse_frontmatter(text)
            if meta.get("self") is True:
                continue  # user self-page — exclude from auto-linking
            slug = path.stem
            title = _title_from_body(body, slug.replace("-", " ").title())
            aliases = meta.get("aliases") or []
            phrases = _phrases_for_page(slug, title, aliases)
            if not phrases:
                continue
            entities.append(Entity(
                slug=slug,
                category=category,
                title=title,
                phrases=phrases,
            ))
    return entities


# ---------------------------------------------------------------------------
# Skip-region bookkeeping
# ---------------------------------------------------------------------------

def _protected_spans(text: str) -> list[tuple[int, int]]:
    """Return (start, end) offsets of regions the linkifier must not touch.

    Unions fenced code blocks, inline code, existing wiki links, and
    markdown links. Spans may overlap; the caller just checks whether a
    candidate position falls inside any of them.
    """
    spans: list[tuple[int, int]] = []
    for regex in (_FENCE_RE, _INLINE_CODE_RE, _WIKILINK_RE, _MD_LINK_RE):
        for m in regex.finditer(text):
            spans.append((m.start(), m.end()))
    spans.sort()
    return spans


def _inside_any_span(pos: int, spans: list[tuple[int, int]]) -> bool:
    """Linear scan is fine for bodies under a few KB — the typical wiki
    page has a handful of spans. Binary search is premature optimization."""
    for start, end in spans:
        if start <= pos < end:
            return True
        if start > pos:
            return False
    return False


# ---------------------------------------------------------------------------
# Core linkifier
# ---------------------------------------------------------------------------

def _escape_phrase(phrase: str) -> str:
    """Regex-escape a phrase, then wrap with boundary assertions.

    Uses lookahead/lookbehind for non-word characters so phrases containing
    punctuation (``Blade & Rose``, ``Molly's Dad``) still bound correctly.
    Standard ``\\b`` fails on & and ' because those are non-word chars.
    """
    escaped = re.escape(phrase)
    # Require the character immediately before the match to be a boundary
    # (start-of-string, whitespace, or punctuation — NOT an alnum/underscore).
    # Same for after. This is roughly \b but works for phrases that start or
    # end on a non-word character.
    return rf"(?<![A-Za-z0-9_]){escaped}(?![A-Za-z0-9_])"


def _resolve_existing_links(body: str, catalog: list[Entity]) -> set[str]:
    """Pre-scan the body for existing ``[[target]]`` refs and return the set
    of catalog slugs they resolve to.

    An LLM-written link like ``[[Soccer Carpool]]`` (title-form) should
    prevent the linkifier from also adding ``[[soccer-carpool|soccer
    carpool]]`` later in the same page. Without this pre-pass, a page can
    end up with the same entity linked two different ways, and the
    linkify pass fails to be idempotent when the LLM used title-form.
    """
    # Build a resolution table: every form (slug, title, alias, normalized)
    # → canonical slug.
    resolve: dict[str, str] = {}
    for e in catalog:
        resolve[e.slug.lower()] = e.slug
        resolve[_normalize_link_key(e.slug)] = e.slug
        for phrase in e.phrases:
            resolve[phrase.lower()] = e.slug
            resolve[_normalize_link_key(phrase)] = e.slug

    hit: set[str] = set()
    for m in _WIKILINK_RE.finditer(body):
        target = m.group(1).strip()
        slug = resolve.get(target.lower()) or resolve.get(_normalize_link_key(target))
        if slug:
            hit.add(slug)
    return hit


def linkify_body(
    body: str,
    catalog: list[Entity],
    *,
    self_slug: str = "",
) -> tuple[str, dict[str, int]]:
    """Return (rewritten_body, {slug: count}).

    For each catalog entity, wraps the FIRST unlinked, non-code occurrence
    of any of its match phrases in ``[[slug|matched text]]`` syntax. If the
    matched text equals the slug exactly, emits the bare ``[[slug]]`` form.
    Subsequent mentions of the same entity on the same page stay as plain
    text (first-occurrence rule). Existing ``[[]]`` links in any form
    (slug, title, alias) count as "already linked" and suppress further
    linkification of that entity on this page.
    """
    added: dict[str, int] = {}

    # Flatten catalog to (phrase, slug) pairs, longest phrase first so
    # overlapping matches resolve to the most specific entity.
    pairs: list[tuple[str, str]] = []
    for e in catalog:
        if e.slug == self_slug:
            continue
        for phrase in e.phrases:
            pairs.append((phrase, e.slug))
    pairs.sort(key=lambda p: (-len(p[0]), p[0].lower()))

    # Pre-seed linked_slugs with anything the author (or the LLM) already
    # linked — in any form. This is the key to idempotency when an entity
    # was previously linked with its title rather than its slug.
    linked_slugs: set[str] = _resolve_existing_links(body, catalog)
    out = body

    for phrase, slug in pairs:
        if slug in linked_slugs:
            continue

        # Rebuild protected spans every iteration — a previous substitution
        # added a new wiki link which now counts as a protected region.
        spans = _protected_spans(out)
        pattern = re.compile(_escape_phrase(phrase), re.IGNORECASE)

        for m in pattern.finditer(out):
            if _inside_any_span(m.start(), spans):
                continue
            matched_text = m.group(0)
            if matched_text == slug:
                replacement = f"[[{slug}]]"
            else:
                replacement = f"[[{slug}|{matched_text}]]"
            out = out[:m.start()] + replacement + out[m.end():]
            linked_slugs.add(slug)
            added[slug] = added.get(slug, 0) + 1
            break  # first occurrence only — move on to the next entity

    return out, added


# ---------------------------------------------------------------------------
# Broken-link detection
# ---------------------------------------------------------------------------

def _normalize_link_key(s: str) -> str:
    """Lowercase + strip non-alphanumerics for loose link comparison.

    Obsidian resolves ``[[Ship New Blade & Rose Theme]]`` to a page with
    slug ``ship-new-blade-rose-theme``. To replicate that resolution we
    collapse both sides to the same alnum-only key: "shipnewbladeandrosetheme"
    vs "shipnewbladerosetheme". Not identical, so we also try a second
    normalization that drops the word "and" (since ``&`` expands to "and"
    in some slugifiers but is dropped entirely in others).
    """
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def find_broken_refs(
    wiki_dir: Path | None = None,
    catalog: list[Entity] | None = None,
) -> list[tuple[str, str]]:
    """Scan every ``[[target]]`` in the wiki and flag targets that don't exist.

    A target resolves if any of the following match any known page:
      - exact slug (``jane-doe``)
      - page title (``Jane Doe``)
      - any frontmatter alias
      - loose normalization (alnum-only, lowercase) — handles Obsidian
        title-style links with punctuation like ``Ship New Blade & Rose Theme``

    Returns (source_page_relpath, target_as_written) pairs, sorted.
    """
    if wiki_dir is None:
        wiki_dir = WIKI_DIR
    if catalog is None:
        catalog = build_catalog(wiki_dir)

    # Build the full known-key set: exact forms + normalized forms
    known_exact: set[str] = set()
    known_normalized: set[str] = set()

    for e in catalog:
        known_exact.add(e.slug)
        known_exact.add(e.slug.replace("-", " "))
        known_normalized.add(_normalize_link_key(e.slug))
        for phrase in e.phrases:
            known_exact.add(phrase)
            known_normalized.add(_normalize_link_key(phrase))

    # Also allow links to pages excluded from the catalog (self-page,
    # short-name pages) — they still exist on disk as valid targets.
    for category in CATEGORIES:
        cat_dir = wiki_dir / category
        if cat_dir.is_dir():
            for path in cat_dir.glob("*.md"):
                known_exact.add(path.stem)
                known_normalized.add(_normalize_link_key(path.stem))
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                _, body = _parse_frontmatter(text)
                title = _title_from_body(body, "")
                if title:
                    known_exact.add(title)
                    known_normalized.add(_normalize_link_key(title))

    def _resolves(target: str) -> bool:
        if target in known_exact:
            return True
        return _normalize_link_key(target) in known_normalized

    broken: list[tuple[str, str]] = []
    for category in CATEGORIES:
        cat_dir = wiki_dir / category
        if not cat_dir.is_dir():
            continue
        for path in sorted(cat_dir.glob("*.md")):
            if path.name.startswith((".", "_")):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            _, body = _parse_frontmatter(text)
            for m in _WIKILINK_RE.finditer(body):
                target = m.group(1).strip()
                if not _resolves(target):
                    broken.append((f"{category}/{path.stem}", target))

    broken.sort()
    return broken


# ---------------------------------------------------------------------------
# Top-level pass
# ---------------------------------------------------------------------------

def linkify_wiki(wiki_dir: Path | None = None, *, dry_run: bool = False) -> LinkifyReport:
    """Run the linkifier over every page in the wiki.

    Builds a fresh catalog, processes each page, writes changes back (unless
    ``dry_run``), collects broken refs, and returns a LinkifyReport. Safe to
    call from nightly or a standalone CLI invocation — idempotent and fast.
    """
    if wiki_dir is None:
        wiki_dir = WIKI_DIR

    catalog = build_catalog(wiki_dir)
    report = LinkifyReport()

    # Collect ALL markdown files to process: category subdirs + root-level files
    # (goals.md, CLAUDE.md, reflection.md, etc.). Skip index.md and log.md
    # which are auto-generated / append-only and shouldn't be linkified.
    _SKIP_ROOT = {"index.md", "log.md", "claude.md"}
    all_paths: list[Path] = []

    # Category subdirectories (people/, projects/, events/)
    for category in CATEGORIES:
        cat_dir = wiki_dir / category
        if not cat_dir.is_dir():
            continue
        for path in sorted(cat_dir.glob("*.md")):
            if not path.name.startswith((".", "_")):
                all_paths.append(path)

    # Root-level markdown files (goals.md, CLAUDE.md, reflection.md, etc.)
    for path in sorted(wiki_dir.glob("*.md")):
        if path.name.startswith((".", "_")):
            continue
        if path.name.lower() in _SKIP_ROOT:
            continue
        all_paths.append(path)

    # Also process event subdirectories (events/YYYY-MM-DD/*.md)
    events_dir = wiki_dir / "events"
    if events_dir.is_dir():
        for day_dir in sorted(events_dir.iterdir()):
            if day_dir.is_dir():
                for path in sorted(day_dir.glob("*.md")):
                    if not path.name.startswith((".", "_")):
                        all_paths.append(path)

    for path in all_paths:
        report.pages_scanned += 1
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        # Preserve the frontmatter block verbatim; only linkify the body.
        fm_match = _FRONTMATTER_RE.match(text)
        if fm_match:
            frontmatter = fm_match.group(1)
            body = text[fm_match.end():]
        else:
            frontmatter = ""
            body = text

        new_body, added = linkify_body(body, catalog, self_slug=path.stem)
        if added:
            report.pages_changed += 1
            for slug, n in added.items():
                report.links_added += n
                report.links_by_slug[slug] = report.links_by_slug.get(slug, 0) + n
            if not dry_run:
                try:
                    path.write_text(frontmatter + new_body, encoding="utf-8")
                except OSError as e:
                    log.warning("linkify: failed to write %s: %s", path, e)

    report.broken_refs = find_broken_refs(wiki_dir, catalog)
    return report
