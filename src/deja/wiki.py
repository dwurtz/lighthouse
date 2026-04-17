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

import yaml

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


def _strip_leading_frontmatter(body: str) -> str:
    """Defensive: if `body` starts with a ``---\\n...\\n---\\n`` block, drop it.

    Structural guard for the new integrate output contract where
    ``body_markdown`` must NOT include a YAML frontmatter block — the
    write path owns frontmatter now. If the model slips and emits one
    anyway, we strip it rather than writing it and clobbering whatever
    was there before.
    """
    if not body:
        return body
    m = _FRONTMATTER_RE.match(body)
    if m:
        return body[m.end():].lstrip("\n")
    # Also catch the one-line corruption shape here, so body_markdown
    # that accidentally starts with `---date: ...---` can't leak onto
    # the page.
    m = _ONELINE_FRONTMATTER_RE.match(body)
    if m:
        return body[m.end():].lstrip("\n")
    return body


def _serialize_event_yaml(meta: dict) -> str:
    """Build the event frontmatter block from `event_metadata`.

    Produces the multi-line shape the rest of the codebase expects:

        date: 2026-04-16
        time: "14:30"
        people: [sam-lee]
        projects: [q2-roadmap]

    `time` is always a double-quoted string (empty → ``""``). List fields
    are flat inline slug lists. Never raises — missing/odd fields fall
    back to safe defaults.
    """
    date = str(meta.get("date") or datetime.now().strftime("%Y-%m-%d"))
    time_val = meta.get("time", "")
    if time_val is None:
        time_val = ""
    time_str = str(time_val).strip().strip('"').strip("'")

    def _slug_list(key: str) -> str:
        raw = meta.get(key) or []
        if isinstance(raw, str):
            raw = [raw]
        slugs = [slugify(str(s)) for s in raw if str(s).strip()]
        return "[" + ", ".join(slugs) + "]"

    people_str = _slug_list("people")
    projects_str = _slug_list("projects")

    return (
        f"date: {date}\n"
        f'time: "{time_str}"\n'
        f"people: {people_str}\n"
        f"projects: {projects_str}"
    )


def _read_existing_frontmatter(path: Path) -> str:
    """Read `path`, extract its YAML frontmatter as a canonical multi-line
    block (without the surrounding ``---`` fences).

    Returns ``""`` if the file doesn't exist, has no frontmatter, or the
    YAML is empty. Repairs one-line corruption along the way so the
    splice is always clean.
    """
    if not path.exists():
        return ""
    text = path.read_text()
    # Repair one-line corruption first so extraction succeeds.
    text, _ = canonicalize_frontmatter(text)
    fm_block, _ = extract_frontmatter(text)
    if not fm_block:
        return ""
    # Strip the opening/closing --- fences and any surrounding whitespace.
    inner_lines = [
        ln for ln in fm_block.splitlines()
        if ln.strip() != "---"
    ]
    inner = "\n".join(inner_lines).strip()
    if not inner:
        return ""
    # Parse + re-dump is tempting for canonicalization, but we want
    # verbatim preservation — yaml.safe_load/safe_dump would reorder
    # keys, re-quote strings, and change flow/block style. Returning
    # `inner` as-is matches the task's requirement: if the old YAML has
    # keys, re-serialize them verbatim. We do validate with safe_load
    # so we can log a warning if the existing YAML is malformed (but
    # still write it back unchanged — the user's data wins).
    try:
        yaml.safe_load(inner)
    except yaml.YAMLError:
        log.warning(
            "wiki: existing frontmatter on %s has invalid YAML — "
            "preserving verbatim anyway",
            path,
        )
    return inner


def _synthesize_person_frontmatter(slug: str) -> str:
    """Minimal frontmatter for a newly-created people page."""
    title = slug.replace("-", " ").title()
    return f"preferred_name: {title}"


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


def _coerce_body_and_metadata(upd: dict) -> tuple[str, dict | None]:
    """Extract (body_markdown, event_metadata) from an update dict.

    Accepts the new integrate schema (``body_markdown`` + optional
    ``event_metadata``) and the legacy shape (``content``, possibly with
    a YAML block at the top). Transition-period back-compat is load-
    bearing because:

      * the bundled app may still be running an older integrate prompt,
      * onboarding + contradictions both still write ``content``.

    Returns ``("", None)`` for updates with no usable body.
    """
    body = upd.get("body_markdown")
    meta = upd.get("event_metadata")

    if body is None:
        # Legacy: ``content`` might carry a ---YAML--- block plus body.
        # For people/projects we drop any leading frontmatter (ownership
        # moved elsewhere). For events, best-effort extract the YAML so
        # we can still emit event_metadata.
        legacy = upd.get("content") or ""
        if not legacy:
            return "", meta if isinstance(meta, dict) else None

        if upd.get("category") == "events" and meta is None:
            # Try to pull event metadata out of the old `content` YAML.
            repaired, _ = canonicalize_frontmatter(legacy)
            fm_block, rest = extract_frontmatter(repaired)
            if fm_block:
                inner = "\n".join(
                    ln for ln in fm_block.splitlines()
                    if ln.strip() != "---"
                ).strip()
                try:
                    parsed = yaml.safe_load(inner) or {}
                    if isinstance(parsed, dict):
                        meta = parsed
                except yaml.YAMLError:
                    log.debug(
                        "legacy event content had unparseable YAML — "
                        "writing body only",
                    )
                body = rest
            else:
                body = legacy
        else:
            # People / projects legacy path — strip any YAML the model
            # may have left at the top; frontmatter is owned elsewhere.
            body = _strip_leading_frontmatter(legacy)

    if not isinstance(body, str):
        body = str(body or "")
    # Defensive cleanup on the new-shape path too.
    body = _strip_leading_frontmatter(body)
    if not isinstance(meta, dict):
        meta = None
    return body, meta


def _compose_page(
    category: str,
    slug: str,
    action: str,
    body: str,
    event_meta: dict | None,
) -> str | None:
    """Build the final file text for one update. Returns None to skip."""
    body = body.rstrip()
    if category == "events":
        if not event_meta:
            log.warning(
                "Skipping event update %s/%s — missing event_metadata "
                "(integrate contract requires it for events)",
                category, slug,
            )
            return None
        yaml_block = _serialize_event_yaml(event_meta)
        return f"---\n{yaml_block}\n---\n{body}\n"

    # people / projects: preserve existing frontmatter verbatim.
    path = _resolve_page_path(category, slug)
    existing = _read_existing_frontmatter(path)
    if existing:
        return f"---\n{existing}\n---\n{body}\n"

    # No existing frontmatter.
    if action == "create" and category == "people":
        return f"---\n{_synthesize_person_frontmatter(slug)}\n---\n{body}\n"
    # Projects or a stray update on a page without prior YAML:
    # write an empty frontmatter block so the structural shape is
    # consistent across the wiki.
    return f"---\n---\n{body}\n"


def _autocreate_referenced_projects(updates: list[dict]) -> list[dict]:
    """Guarantee 'no dangling project slugs' by auto-appending stub
    `create` entries for any event-referenced project that doesn't
    exist on disk AND isn't being created in this same batch.

    The model is told (in the integrate prompt) to pair any novel
    slug with a `create` wiki_update. That's best-effort. This
    function is the structural enforcement — deterministic,
    no-LLM, runs inline before any write hits disk.

    Stubs are minimal: a titleized heading + one-sentence placeholder.
    Next integrate cycle can enrich the page naturally; dedup merges
    near-duplicates on its 3x/day sweep. The point is that the wiki
    is always internally consistent — every `[[slug]]` link resolves.
    """
    # Collect slugs being created in THIS batch (any category).
    batch_creates: set[str] = set()
    for upd in updates:
        if (upd.get("action") or "").lower() == "create" and upd.get("category") == "projects":
            s = upd.get("slug", "").strip()
            if s:
                batch_creates.add(s)

    # Collect every project slug referenced by an event in this batch.
    referenced: list[str] = []
    for upd in updates:
        if upd.get("category") != "events":
            continue
        meta = upd.get("event_metadata") or {}
        for s in (meta.get("projects") or []):
            if isinstance(s, str) and s.strip():
                referenced.append(s.strip())

    # For each referenced slug: exists on disk? in this batch? else stub.
    appended: set[str] = set()
    new_stubs: list[dict] = []
    projects_dir = WIKI_DIR / "projects"
    for slug in referenced:
        if slug in batch_creates or slug in appended:
            continue
        if (projects_dir / f"{slug}.md").exists():
            continue
        # Auto-create a stub.
        title = slug.replace("-", " ").title()
        body = (
            f"# {title}\n\n"
            f"*Auto-created from event reference. Refine in the next cycle.*\n\n"
            f"## Recent\n"
        )
        new_stubs.append({
            "category": "projects",
            "slug": slug,
            "action": "create",
            "body_markdown": body,
            "reason": (
                "auto-synthesized stub — an event in this batch referenced "
                f"projects/{slug} but no page existed and no explicit create was emitted"
            ),
        })
        appended.add(slug)

    if new_stubs:
        log.info(
            "wiki: auto-created %d stub project page(s) to resolve dangling "
            "event refs: %s",
            len(new_stubs),
            ", ".join(s["slug"] for s in new_stubs),
        )
        # Prepend so the project creates run before the event writes that
        # reference them — the order doesn't affect correctness (filesystem
        # sees both either way) but it matches natural causality.
        return new_stubs + list(updates)
    return updates


def apply_updates(updates: list[dict]) -> int:
    """Apply wiki updates returned by the unified analysis call.

    Each update is ``{category, slug, action, body_markdown,
    event_metadata?, reason}``. Legacy ``content`` is still accepted
    (transition-period back-compat) and routed through the same
    splicing path.

    Writes each page, rebuilds the index, refreshes the QMD search
    index, and commits the changes to git. Returns the number of pages
    successfully written.

    Frontmatter authority (2026-04-16 structural change):
      * ``people`` / ``projects`` frontmatter is OWNED by other code
        paths (contact enrichment, onboarding, the user's manual edits).
        Integrate's body is spliced onto the existing YAML verbatim.
      * ``events`` frontmatter IS owned by integrate — synthesized from
        the structured ``event_metadata`` field.

    This eliminates the recurring frontmatter-clobber bug where an
    integrate cycle would drop ``inner_circle: true`` / ``phones`` /
    ``emails`` on rewrite. There is no prompt rule to enforce anymore
    — the authority simply isn't there.
    """
    if not updates:
        return 0

    ensure_dirs()

    # Enforce the "no dangling project slugs" invariant: any event
    # referencing a project slug must resolve to an existing page. If
    # the batch has an event pointing at a slug that (a) doesn't exist
    # on disk and (b) isn't being created in this same batch, auto-
    # append a stub `create` update for it. This guarantees the
    # filesystem never holds a dangling link regardless of what the
    # model emitted. Dedup's 3x/day pass handles any near-duplicate
    # stubs that accumulate over time. See ship-notes 2026-04-17.
    updates = _autocreate_referenced_projects(updates)

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

        body, event_meta = _coerce_body_and_metadata(upd)
        if not body:
            log.warning("Skipping invalid wiki update (no body): %s", upd)
            continue

        composed = _compose_page(category, slug, action, body, event_meta)
        if composed is None:
            continue

        try:
            write_page(category, slug, composed)
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
