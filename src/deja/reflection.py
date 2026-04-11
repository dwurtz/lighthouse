"""Reflection pass — runs a few times a day with Gemini 2.5 Pro.

Two jobs in one LLM call:
  1. Consolidate the wiki — fix contradictions, remove duplication, clean
     up organization, retire stale pages, collapse duplicate wiki-links.
  2. Share thoughts — what stands out, what's worth considering,
     questions worth answering. Written to ``~/Deja/reflection.md``
     for the user to read in the morning.

This is the only place the agent is allowed to speculate. The faster
integration cycles stay tight and factual.

Also runs two deterministic subroutines before the LLM call:
  - contact enrichment (macOS Contacts + Gmail headers into people pages)
  - linkify sweep (wrap unlinked entity mentions after LLM cleanup)

Scheduling logic (slot boundaries, last-run marker, concurrency guard)
lives in ``reflection_scheduler.py``. This module re-exports the public
API so existing ``from deja.reflection import ...`` continues to work.
"""

from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime, timedelta, timezone

from deja import wiki as wiki_store
from deja.config import (
    OBSERVATIONS_LOG,
    REFLECT_MODEL,
    WIKI_DIR,
)
from deja.llm_client import GeminiClient
from deja.prompts import load as load_prompt

# Re-export the public scheduling API so callers that do
# ``from deja.reflection import should_run_reflection, run_reflection``
# continue to work unchanged.
from deja.reflection_scheduler import (  # noqa: F401
    should_run_reflection,
    run_reflection,
    _most_recent_slot,
    _read_last_run,
    _write_last_run,
    REFLECT_SLOT_HOURS,
)

log = logging.getLogger(__name__)

# User-facing morning note
REFLECTION_NOTE = wiki_store.WIKI_DIR / "reflection.md"
_LEGACY_REFLECTION_NOTE = wiki_store.WIKI_DIR / "nightly.md"
if _LEGACY_REFLECTION_NOTE.exists() and not REFLECTION_NOTE.exists():
    try:
        _LEGACY_REFLECTION_NOTE.rename(REFLECTION_NOTE)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Signal / event text builders
# ---------------------------------------------------------------------------

def _recent_signals_text(days: int = 7, max_chars: int = 500_000) -> str:
    """Build the recent-observations block for the reflect prompt."""
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
            text = (s.get("text", "") or "")[:2000]
            lines_out.append(f"[{ts[:19]}] [{source}] {sender}: {text}")
        except Exception:
            continue
    out = "\n".join(lines_out[-10_000:])
    if len(out) > max_chars:
        out = out[-max_chars:]
    return out or "(no recent signals)"


def _recent_events_text(days: int = 7) -> str:
    """Read all event pages from the last N days and format for the reflect prompt."""
    events_dir = WIKI_DIR / "events"
    if not events_dir.is_dir():
        return "(no events yet)"

    from datetime import date as _date
    today = _date.today()
    cutoff = today - timedelta(days=days)
    entries: list[tuple[str, str]] = []

    for day_dir in sorted(events_dir.iterdir()):
        if not day_dir.is_dir():
            continue
        try:
            dir_date = _date.fromisoformat(day_dir.name)
        except ValueError:
            continue
        if dir_date < cutoff:
            continue
        for event_file in sorted(day_dir.glob("*.md")):
            try:
                content = event_file.read_text(encoding="utf-8", errors="replace")
                entries.append((f"{day_dir.name}/{event_file.stem}", content.strip()))
            except OSError:
                continue

    if not entries:
        return "(no events in the last 7 days)"

    lines = []
    for slug, content in entries:
        lines.append(f"### events/{slug}\n\n{content}")
    return "\n\n---\n\n".join(lines)


# ---------------------------------------------------------------------------
# Goal-action execution helper
# ---------------------------------------------------------------------------

def _execute_calendar_create(params: dict, reason: str) -> None:
    """Create a Google Calendar event via gws."""
    summary = params.get("summary", "")
    start = params.get("start", "")
    end = params.get("end", "")
    if not summary or not start or not end:
        log.warning("calendar_create: missing required params (summary/start/end)")
        return

    event_body: dict = {
        "summary": summary,
        "start": {"dateTime": start},
        "end": {"dateTime": end},
    }
    if params.get("location"):
        event_body["location"] = params["location"]
    if params.get("description"):
        event_body["description"] = params["description"]

    try:
        result = subprocess.run(
            [
                "gws", "calendar", "events", "insert",
                "--params", json.dumps({"calendarId": "primary"}),
                "--json", json.dumps(event_body),
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0:
            log.info("calendar_create: created '%s' at %s — %s", summary, start, reason)
            try:
                from deja.activity_log import append_log_entry
                append_log_entry("reflect", f"created calendar event: {summary} at {start}")
            except Exception:
                pass
        else:
            log.warning("calendar_create failed: %s", result.stderr[:200])
    except Exception:
        log.exception("calendar_create subprocess failed")


# Contact enrichment helpers live in reflection_enrichment.py
from deja.reflection_enrichment import (
    find_orphan_people_with_contacts as _find_orphan_people_with_contacts,
    format_orphan_candidates as _format_orphan_candidates,
)


def _delete_page(category: str, slug: str):
    if wiki_store.delete_page(category, slug):
        log.info("Reflection deleted page: %s/%s", category, slug)


# ---------------------------------------------------------------------------
# Reflection pipeline body (called by reflection_scheduler.run_reflection)
# ---------------------------------------------------------------------------

async def _run_reflection_body() -> dict:
    """The real work of the reflection pass. Lock-free — call via run_reflection."""
    wiki_store.ensure_dirs()

    try:
        from deja.wiki_catalog import rebuild_index
        rebuild_index()
    except Exception:
        log.debug("index rebuild at reflection start failed", exc_info=True)

    # Enrich existing people pages with contact info
    enrichment_report = None
    try:
        from deja.people_enrichment import enrich_people_pages
        enrichment_report = enrich_people_pages()
        log.info("%s", enrichment_report.brief())
    except Exception:
        log.exception("contact enrichment failed")

    wiki_text = wiki_store.render_for_prompt()

    from deja.identity import load_user
    user_fields = load_user().as_prompt_fields()

    prompt = load_prompt("deduplicate").format(
        current_time=datetime.now().strftime("%A, %B %d, %Y — %H:%M"),
        wiki_text=wiki_text,
        **user_fields,
    )

    log.info(
        "Deduplicate: running Pro with %d chars of context (wiki=%d)",
        len(prompt), len(wiki_text),
    )

    gemini = GeminiClient()
    resp_text = await gemini._generate(
        model=REFLECT_MODEL,
        contents=prompt,
        config_dict={
            "response_mime_type": "application/json",
            "max_output_tokens": 65536,
            "temperature": 0.3,
        },
    )
    data = json.loads(resp_text)

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

    # Execute goal_actions
    goal_actions = data.get("goal_actions") or []
    actions_executed = 0
    if goal_actions:
        try:
            from deja.goal_actions import execute_all
            actions_executed = execute_all(goal_actions)
        except Exception:
            log.exception("reflect goal_actions failed")

    # Update goals.md
    tasks_update = data.get("tasks_update")
    if tasks_update:
        try:
            from deja.goals import apply_tasks_update
            changes = apply_tasks_update(tasks_update)
            if changes:
                log.info("reflect: updated %d item(s) in goals.md", changes)
        except Exception:
            log.exception("reflect tasks_update failed")

    # Deterministic linkify pass
    linkify_report = None
    try:
        from deja.wiki_linkify import linkify_wiki
        linkify_report = linkify_wiki()
        log.info("%s", linkify_report.brief())
    except Exception:
        log.exception("linkify pass failed")

    # Refresh QMD, rebuild index.md, and commit to git
    linkified_pages = linkify_report.pages_changed if linkify_report else 0
    enriched_pages = enrichment_report.pages_changed if enrichment_report else 0
    if applied > 0 or linkified_pages > 0 or enriched_pages > 0:
        try:
            from deja.wiki_catalog import rebuild_index
            rebuild_index()
        except Exception:
            pass
        try:
            from deja.llm.search import refresh_index
            refresh_index()
        except Exception:
            pass
        try:
            subprocess.run(["qmd", "update"], capture_output=True, timeout=30)
            subprocess.run(["qmd", "embed"], capture_output=True, timeout=120)
            log.info("QMD index + embeddings refreshed")
        except Exception:
            log.debug("QMD refresh failed", exc_info=True)
        try:
            from deja.wiki_git import commit_changes
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

    # Append thoughts to reflection.md
    if thoughts:
        header = f"# {datetime.now().strftime('%A, %B %d, %Y')}\n\n"
        existing = REFLECTION_NOTE.read_text() if REFLECTION_NOTE.exists() else "# Reflection Notes\n\n*Short notes from your assistant after each reflection pass. Newest on top.*\n\n"
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
        from deja.activity_log import append_log_entry
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

        if enrichment_report is not None:
            for change in enrichment_report.changes[:20]:
                append_log_entry("reflect", f"enriched {change.brief()}")
            for slug in enrichment_report.ambiguous[:10]:
                append_log_entry(
                    "reflect",
                    f"ambiguous macos contact for {slug} — multiple matches, "
                    f"add a unique alias to resolve",
                )
        if linkify_report is not None:
            for src, target in linkify_report.broken_refs[:20]:
                append_log_entry(
                    "reflect",
                    f"broken wiki link: {src} → [[{target}]] (target missing)",
                )
    except Exception:
        pass

    return data
