"""Reflection pass — runs a few times a day with Gemini 2.5 Pro.

Two jobs in one LLM call:
  1. Consolidate the wiki — fix contradictions, remove duplication, clean
     up organization, retire stale pages, collapse duplicate wiki-links.
  2. Share thoughts — what stands out, what's worth considering,
     questions worth answering. Written to ``~/Lighthouse/reflection.md``
     for the user to read in the morning.

This is the only place the agent is allowed to speculate. The faster
integration cycles stay tight and factual.

Also runs two deterministic subroutines before the LLM call:
  - contact enrichment (macOS Contacts + Gmail headers into people pages)
  - linkify sweep (wrap unlinked entity mentions after LLM cleanup)
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lighthouse import wiki as wiki_store
from lighthouse.config import (
    OBSERVATIONS_LOG,
    REFLECT_MODEL,
    REFLECT_SLOT_HOURS,
    WIKI_DIR,
    LIGHTHOUSE_HOME,
)
from lighthouse.llm_client import GeminiClient, types
from lighthouse.prompts import load as load_prompt

log = logging.getLogger(__name__)

# User-facing morning note — renamed from nightly.md for consistency with
# the reflect/integrate/observe vocabulary. If an old nightly.md exists on
# disk, rename it once.
REFLECTION_NOTE = wiki_store.WIKI_DIR / "reflection.md"
_LEGACY_REFLECTION_NOTE = wiki_store.WIKI_DIR / "nightly.md"
if _LEGACY_REFLECTION_NOTE.exists() and not REFLECTION_NOTE.exists():
    try:
        _LEGACY_REFLECTION_NOTE.rename(REFLECTION_NOTE)
    except OSError:
        pass

# Persistent marker of the last successful reflection run. The agent
# loop checks this on startup and at the start of every integration
# cycle — if the last run predates the most recent 02:00 wall-clock
# threshold, reflection is triggered inline. Simple, clock-aligned, no
# drift, survives macOS maintenance sleep.
_LAST_RUN_FILE = LIGHTHOUSE_HOME / "last_reflection_run"
_LEGACY_LAST_RUN = LIGHTHOUSE_HOME / "last_nightly_run"
if _LEGACY_LAST_RUN.exists() and not _LAST_RUN_FILE.exists():
    try:
        _LEGACY_LAST_RUN.rename(_LAST_RUN_FILE)
    except OSError:
        pass

# Slot hours come from config (default 02:00 / 11:00 / 18:00). Reflection
# runs once per slot: on the first agent heartbeat after any slot hour
# that observes the previous run predates that slot. Three slots give
# stale commitments an ~8h ceiling before Pro revisits them, while still
# keeping one slot safely in the overnight window.


def _most_recent_slot(now: datetime) -> datetime:
    """Return the most recent reflect slot boundary at or before ``now``.

    Walks the configured ``REFLECT_SLOT_HOURS`` in local time. If any of
    today's slots is <= now, returns the latest one. Otherwise returns
    yesterday's last slot (the clock hasn't crossed today's earliest slot
    yet, so the "current" slot is still yesterday's final one).
    """
    if not REFLECT_SLOT_HOURS:
        # Degenerate config — pretend we just ran, don't trigger.
        return now
    today_slots = [
        now.replace(hour=h, minute=0, second=0, microsecond=0)
        for h in REFLECT_SLOT_HOURS
    ]
    past = [s for s in today_slots if s <= now]
    if past:
        return past[-1]
    # Wrap to yesterday's last slot
    return today_slots[-1] - timedelta(days=1)

# Lock that serializes reflection runs. Guards against a rare race
# where two consecutive integration cycles both observe the threshold
# crossed and try to run reflection at almost the same time. The
# second caller sees the lock held and returns immediately — two
# back-to-back reflection calls would just waste Pro tokens for the
# same result.
_run_lock = asyncio.Lock()


def _recent_signals_text(days: int = 7, max_chars: int = 6000) -> str:
    path = OBSERVATIONS_LOG
    if not path.exists():
        return "(no signals)"
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    lines_out: list[str] = []
    for line in path.read_text().splitlines():
        try:
            s = json.loads(line)
            ts = s.get("timestamp", "")
            try:
                t = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
            except Exception:
                continue
            if t < cutoff:
                continue
            source = s.get("source", "?")
            sender = s.get("sender", "")
            text = (s.get("text", "") or "")[:200]
            lines_out.append(f"[{ts[:19]}] [{source}] {sender}: {text}")
        except Exception:
            continue
    out = "\n".join(lines_out[-400:])
    if len(out) > max_chars:
        out = out[-max_chars:]
    return out or "(no recent signals)"


_NAME_RE = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2})\b")
_EMAIL_RE = re.compile(r"<([^>]+@[^>\s]+)>|([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})")


def _find_orphan_people_with_contacts() -> list[dict]:
    """Scan project pages for person-like names that lack their own people/ page,
    then look up each in macOS Contacts and recent email signals. Returns a list
    of candidates: [{name, slug, phones, emails, mentioned_in, context}].

    The reflection LLM decides which of these warrant a new people/ page based on
    substance of the mention and quality of the contact info found. Candidates
    with no contact info are still included (the model can choose to skip them).
    """
    if not (WIKI_DIR / "projects").exists():
        return []

    existing_people = {p.stem for p in (WIKI_DIR / "people").glob("*.md")}
    existing_projects = {p.stem for p in (WIKI_DIR / "projects").glob("*.md")}

    # Gather person-like name mentions across project pages
    mentions: dict[str, list[str]] = {}
    for proj_path in (WIKI_DIR / "projects").glob("*.md"):
        text = proj_path.read_text()
        # Strip [[wiki links]] — those are already handled as entity references
        clean = re.sub(r"\[\[[^\]]+\]\]", "", text)
        for m in _NAME_RE.finditer(clean):
            name = m.group(1)
            mentions.setdefault(name, []).append(proj_path.stem)

    def _slugify(s: str) -> str:
        return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "unnamed"

    # Filter: name can't already have a people or project page, and must be
    # plausibly a person (filter out obvious non-names).
    NON_PEOPLE = {
        "blade rose", "highland cow", "peter rabbit", "superman saves",
        "new york", "los angeles", "palo alto", "san francisco", "united states",
        "google onboarding", "mayo clinic", "executive health",
    }
    orphans: dict[str, list[str]] = {}
    for name, projs in mentions.items():
        slug = _slugify(name)
        if slug in existing_people or slug in existing_projects:
            continue
        if name.lower() in NON_PEOPLE:
            continue
        orphans[name] = projs

    if not orphans:
        return []

    # Look up macOS Contacts (cached in-memory index)
    from lighthouse.observations import contacts as contacts_mod
    if contacts_mod._name_set is None:
        contacts_mod._build_index()
    name_set = contacts_mod._name_set or set()
    phone_index = contacts_mod._phone_index or {}

    # Scan last ~90 days of signal_log for email mentions of each orphan
    email_hits: dict[str, set[str]] = {}
    context_hits: dict[str, str] = {}
    if OBSERVATIONS_LOG.exists():
        for line in OBSERVATIONS_LOG.read_text().splitlines()[-5000:]:
            line = line.strip()
            if not line:
                continue
            try:
                sig = json.loads(line)
            except json.JSONDecodeError:
                continue
            if sig.get("source") != "email":
                continue
            sender = sig.get("sender", "") or ""
            body = sig.get("text", "") or ""
            blob = f"{sender}\n{body}"
            for name in orphans:
                if name.lower() in blob.lower():
                    # Extract emails from sender/body
                    for m in _EMAIL_RE.finditer(blob):
                        addr = m.group(1) or m.group(2)
                        if addr:
                            email_hits.setdefault(name, set()).add(addr)
                    # Capture a short context snippet on first hit
                    if name not in context_hits:
                        context_hits[name] = body[:160]

    candidates = []
    for name, projs in sorted(orphans.items()):
        phones = [p for p, n in phone_index.items() if n.lower() == name.lower()][:3]
        emails = sorted(email_hits.get(name, set()))[:3]
        in_contacts = name.lower() in name_set
        if not (phones or emails or in_contacts):
            # Skip names we know nothing about — the model can't do anything useful
            continue
        candidates.append({
            "name": name,
            "slug": _slugify(name),
            "phones": phones,
            "emails": emails,
            "in_macos_contacts": in_contacts,
            "mentioned_in": sorted(set(projs))[:5],
            "context_snippet": context_hits.get(name, ""),
        })

    log.info("Reflection enrichment: found %d orphan people candidates with contact info",
             len(candidates))
    return candidates


def _format_orphan_candidates(candidates: list[dict]) -> str:
    """Format orphan candidates for injection into the reflect prompt."""
    if not candidates:
        return "(none — every person mentioned in projects already has a page or no contact info was found)"
    lines = []
    for c in candidates:
        bits = [f"**{c['name']}** (slug: `{c['slug']}`)"]
        if c["phones"]:
            bits.append(f"phones: {', '.join(c['phones'])}")
        if c["emails"]:
            bits.append(f"emails: {', '.join(c['emails'])}")
        if c["in_macos_contacts"]:
            bits.append("in macOS Contacts")
        bits.append(f"mentioned in: {', '.join(c['mentioned_in'])}")
        if c["context_snippet"]:
            bits.append(f"context: {c['context_snippet']}")
        lines.append("- " + " · ".join(bits))
    return "\n".join(lines)


def _delete_page(category: str, slug: str):
    # Thin wrapper kept for backward compat — the real logic (backup +
    # unlink + log) now lives in wiki.delete_page so both the 5-minute
    # integrate cycle and the nightly reflect pass take the same path.
    if wiki_store.delete_page(category, slug):
        log.info("Reflection deleted page: %s/%s", category, slug)


# ---------------------------------------------------------------------------
# Last-run marker and catch-up logic
# ---------------------------------------------------------------------------

def _read_last_run() -> datetime | None:
    """Return the timestamp of the last successful reflection run, or None."""
    try:
        raw = _LAST_RUN_FILE.read_text().strip()
    except (OSError, FileNotFoundError):
        return None
    try:
        ts = datetime.fromisoformat(raw)
    except ValueError:
        log.warning("last_reflection_run file has unparseable content: %r", raw)
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def _write_last_run(ts: datetime | None = None) -> None:
    """Record `ts` (or now) as the last successful reflection run."""
    if ts is None:
        ts = datetime.now(timezone.utc)
    try:
        LIGHTHOUSE_HOME.mkdir(parents=True, exist_ok=True)
        _LAST_RUN_FILE.write_text(ts.isoformat())
    except OSError:
        log.exception("Failed to write last_reflection_run marker")


def should_run_reflection(now: datetime | None = None) -> bool:
    """Return True if reflection hasn't run since the most recent slot boundary.

    Clock-aligned, not interval-based. With default slots (02:00, 11:00,
    18:00), "did reflection run since the last time the clock crossed any
    slot boundary?" means:

      • It's 12:00 today, last run was today at 03:00 → run (last run
        predates today's 11:00 slot that just passed)
      • It's 12:00 today, last run was today at 11:30 → don't run
        (last run is past today's most recent slot)
      • It's 01:30 today, last run was yesterday at 19:00 → don't run
        (last run is past yesterday's final slot of 18:00; today's 02:00
        hasn't happened yet)
      • Machine was asleep all day; wakes at 20:00 with last run 6 days
        ago → run ONCE (backs up to today's 18:00 slot, not a stampede)

    Properties:
      • Runs at most once per slot
      • No drift from repeated runs (a pure 8h timer creeps earlier)
      • First agent heartbeat past any slot hour runs reflection; sleep
        past one or more slots just fires the one catch-up on wake
      • Survives macOS maintenance sleep

    All times are local — slot boundaries are wall-clock hours, not UTC.
    ``last_reflection_run`` is stored as an aware UTC timestamp and
    converted for comparison.
    """
    if now is None:
        now = datetime.now().astimezone()
    elif now.tzinfo is None:
        now = now.astimezone()

    last = _read_last_run()
    if last is None:
        return True

    threshold = _most_recent_slot(now)
    return last.astimezone(threshold.tzinfo) < threshold


async def run_reflection() -> dict:
    """Run one reflection pass. Returns the parsed LLM output.

    Concurrent invocations are coalesced: the second caller sees the
    lock held and returns an empty result immediately rather than
    waiting or double-running Pro. This matters because two adjacent
    analysis cycles can both observe the 02:00 threshold crossed at
    almost the same time. On successful completion, the
    `last_reflection_run` marker is updated so `should_run_reflection`
    knows today's slot is filled.
    """
    if _run_lock.locked():
        log.info("Reflection already running — skipping concurrent invocation")
        return {"wiki_updates": [], "thoughts": "", "skipped": "concurrent"}

    async with _run_lock:
        try:
            result = await _run_reflection_body()
        except Exception:
            log.exception("Reflection failed — not updating last-run marker")
            return {"wiki_updates": [], "thoughts": "", "error": True}
        # Only update the marker on a clean run — on LLM failure the
        # body re-raises, we log, and leave the marker so the next
        # heartbeat retries. A legitimate "nothing to do" pass (empty
        # updates list, no thoughts) still counts as successful.
        _write_last_run()
        return result


async def _run_reflection_body() -> dict:
    """The real work of the reflection pass. Lock-free — call via run_reflection."""
    wiki_store.ensure_dirs()

    # Rebuild the index up front so we start from an accurate snapshot —
    # catches any manual deletes or Obsidian edits since the last cycle.
    try:
        from lighthouse.wiki_catalog import rebuild_index
        rebuild_index()
    except Exception:
        log.debug("index rebuild at reflection start failed", exc_info=True)

    # Enrich existing people pages with contact info from macOS Contacts
    # and Gmail headers. Runs BEFORE the LLM call so Pro sees the enriched
    # frontmatter in the wiki context — phone/email/company fields feed
    # into its prose and grounding decisions.
    enrichment_report = None
    try:
        from lighthouse.people_enrichment import enrich_people_pages
        enrichment_report = enrich_people_pages()
        log.info("%s", enrichment_report.brief())
    except Exception:
        log.exception("contact enrichment failed")

    wiki_text = wiki_store.render_for_prompt()
    signals_text = _recent_signals_text()

    from lighthouse.observations.contacts import get_contacts_summary
    contacts_text = get_contacts_summary()

    # Enrichment: orphan people candidates with pre-looked-up contact info.
    # Pro decides which (if any) warrant a new people/ page.
    orphan_candidates = _find_orphan_people_with_contacts()
    orphan_text = _format_orphan_candidates(orphan_candidates)

    from lighthouse.wiki_schema import load_schema
    schema = load_schema()

    from lighthouse.identity import load_user
    user_fields = load_user().as_prompt_fields()

    prompt = load_prompt("reflect").format(
        current_time=datetime.now().strftime("%A, %B %d, %Y — %H:%M"),
        contacts_text=contacts_text,
        schema=schema,
        wiki_text=wiki_text,
        recent_observations=signals_text,
        orphan_people=orphan_text,
        **user_fields,
    )

    log.info("Reflection: running Pro with %d chars of context", len(prompt))

    gemini = GeminiClient()
    # Let exceptions propagate to the wrapper — that's how the
    # catch-up path knows the marker shouldn't be updated and the
    # next heartbeat should retry.
    resp = await gemini.client.aio.models.generate_content(
        model=REFLECT_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            max_output_tokens=32000,
            temperature=0.3,
        ),
    )
    data = json.loads(resp.text)

    updates = data.get("wiki_updates", []) or []
    thoughts = (data.get("thoughts") or "").strip()

    # Apply wiki updates, including deletes
    applied = 0
    for u in updates:
        action = u.get("action", "update")
        category = u.get("category")
        slug = u.get("slug", "")
        if action == "delete":
            try:
                _delete_page(category, slug)
                applied += 1
            except Exception:
                log.exception("delete failed: %s/%s", category, slug)
        else:
            content = u.get("content", "")
            if category and slug and content:
                try:
                    wiki_store.write_page(category, slug, content)
                    applied += 1
                    log.info("Reflect %s: %s/%s — %s",
                             action, category, slug, (u.get("reason") or "")[:80])
                except Exception:
                    log.exception("write failed: %s/%s", category, slug)

    # Deterministic linkify pass — catches any entity mentions the LLM
    # left as plain text. Runs AFTER the LLM updates so the catalog
    # includes any pages reflection just created, and BEFORE the git commit
    # so linkification lands in the same commit as the cleanup.
    linkify_report = None
    try:
        from lighthouse.wiki_linkify import linkify_wiki
        linkify_report = linkify_wiki()
        log.info("%s", linkify_report.brief())
    except Exception:
        log.exception("linkify pass failed")

    # Refresh QMD, rebuild index.md, and commit to git
    linkified_pages = linkify_report.pages_changed if linkify_report else 0
    enriched_pages = enrichment_report.pages_changed if enrichment_report else 0
    if applied > 0 or linkified_pages > 0 or enriched_pages > 0:
        try:
            from lighthouse.wiki_catalog import rebuild_index
            rebuild_index()
        except Exception:
            pass
        try:
            from lighthouse.llm.search import refresh_index
            refresh_index()
        except Exception:
            pass
        try:
            from lighthouse.wiki_git import commit_changes
            msg_parts = []
            if enriched_pages > 0:
                msg_parts.append(f"enriched {enriched_pages} contact page(s)")
            if applied > 0:
                msg_parts.append(f"cleaned up {applied} page(s)")
            if linkified_pages > 0 and linkify_report is not None:
                msg_parts.append(
                    f"linkified {linkify_report.links_added} ref(s) on {linkified_pages} page(s)"
                )
            commit_changes(f"reflect: {', '.join(msg_parts) or 'no-op'}")
        except Exception:
            pass

    # Append thoughts to reflection.md — David reads this in Obsidian in the morning
    if thoughts:
        header = f"# {datetime.now().strftime('%A, %B %d, %Y')}\n\n"
        existing = REFLECTION_NOTE.read_text() if REFLECTION_NOTE.exists() else "# Reflection Notes\n\n*Short notes from your assistant after each reflection pass. Newest on top.*\n\n"
        # Insert newest entry right after the top preamble (before any existing dated sections).
        top, sep, rest = existing.partition("\n\n---\n\n")
        new_entry = header + thoughts + "\n\n---\n\n"
        if "\n## " in existing or rest:
            REFLECTION_NOTE.write_text(top + "\n\n---\n\n" + new_entry + (rest or ""))
        else:
            REFLECTION_NOTE.write_text(existing.rstrip() + "\n\n---\n\n" + new_entry)

    log.info("Reflection done: %d pages changed, %d chars of thoughts",
             applied, len(thoughts))

    # Human-readable log entry in the wiki
    try:
        from lighthouse.activity_log import append_log_entry
        summary_parts = []
        if enrichment_report is not None and enrichment_report.pages_changed > 0:
            summary_parts.append(
                f"enriched {enrichment_report.pages_changed} contact page(s)"
            )
        if applied > 0:
            summary_parts.append(f"{applied} page(s) updated")
        if linkify_report is not None and linkify_report.pages_changed > 0:
            summary_parts.append(
                f"linkified {linkify_report.links_added} ref(s) on "
                f"{linkify_report.pages_changed} page(s)"
            )
        if linkify_report is not None and linkify_report.broken_refs:
            summary_parts.append(f"{len(linkify_report.broken_refs)} broken ref(s)")
        if enrichment_report is not None and enrichment_report.ambiguous:
            summary_parts.append(
                f"{len(enrichment_report.ambiguous)} ambiguous macos match(es)"
            )
        if thoughts:
            summary_parts.append(f"{len(thoughts)} chars of thoughts written to reflection.md")
        if not summary_parts:
            summary_parts.append("nothing to flag")
        append_log_entry("reflect", "; ".join(summary_parts))

        # Per-enrichment detail entries — greppable in Obsidian
        if enrichment_report is not None:
            for change in enrichment_report.changes[:20]:
                append_log_entry("reflect", f"enriched {change.brief()}")
            for slug in enrichment_report.ambiguous[:10]:
                append_log_entry(
                    "reflect",
                    f"ambiguous macos contact for {slug} — multiple matches, "
                    f"add a unique alias to resolve",
                )
        # Emit a separate log entry for each broken link so it's greppable
        if linkify_report is not None:
            for src, target in linkify_report.broken_refs[:20]:
                append_log_entry(
                    "reflect",
                    f"broken wiki link: {src} → [[{target}]] (target missing)",
                )
    except Exception:
        pass

    return data
