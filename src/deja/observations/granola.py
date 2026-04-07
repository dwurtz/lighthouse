"""Granola meeting-notes enrichment layer.

Reads the local Granola Electron app cache at
``~/Library/Application Support/Granola/cache-v6.json`` — no API key
needed. The cache mirrors the server-side data and contains meeting
notes, transcripts, and attendee info.

**Google Calendar is the source of truth for meetings** (what happened,
who was there, when). Granola is the *enrichment layer* that attaches
notes and transcripts to calendar events. In the onboarding path,
``enrich_calendar_observations`` joins Granola docs onto calendar-
sourced Observations by timestamp + attendee overlap, appending notes
to existing observations rather than emitting independently.

Three entry points:

  * ``collect_recent_granola`` — steady-state, called every few cycles
    by the monitor. Returns recently-updated Granola notes as standalone
    observations (the steady-state calendar collector handles the
    "meeting happened" signal separately).
  * ``enrich_calendar_observations`` — joins Granola notes onto a list
    of calendar-sourced Observations for the onboarding backfill path.
  * ``fetch_granola_backfill`` — standalone onboarding path for Granola
    docs that DON'T match any calendar event (personal notes, meetings
    on other people's calendars, etc.).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from deja.observations.types import Observation

log = logging.getLogger(__name__)


CACHE_PATH = Path.home() / "Library" / "Application Support" / "Granola" / "cache-v6.json"


def _load_documents() -> dict[str, dict]:
    """Read the cache and return the documents dict, or {} on failure."""
    if not CACHE_PATH.exists():
        return {}
    try:
        with open(CACHE_PATH) as f:
            data = json.load(f)
        return data.get("cache", {}).get("state", {}).get("documents", {})
    except (json.JSONDecodeError, OSError) as e:
        log.warning("Failed to read Granola cache: %s", e)
        return {}


def _load_transcripts() -> dict[str, list[dict]]:
    """Load transcripts keyed by document ID."""
    if not CACHE_PATH.exists():
        return {}
    try:
        with open(CACHE_PATH) as f:
            data = json.load(f)
        return data.get("cache", {}).get("state", {}).get("transcripts", {})
    except (json.JSONDecodeError, OSError):
        return {}


def _parse_iso(ts: str | None) -> datetime | None:
    """Parse an ISO timestamp, returning None on failure."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.astimezone().replace(tzinfo=None)
    except (ValueError, TypeError):
        return None


def _attendee_names(doc: dict) -> list[str]:
    """Extract attendee display names from the calendar event, skipping
    the user themselves (identified as the creator)."""
    cal = doc.get("google_calendar_event") or {}
    attendees = cal.get("attendees") or []
    creator_email = ""
    people = doc.get("people") or {}
    if isinstance(people, dict):
        creator = people.get("creator") or {}
        creator_email = (creator.get("email") or "").lower()

    names: list[str] = []
    for a in attendees:
        email = (a.get("email") or "").lower()
        if email == creator_email:
            continue
        name = a.get("displayName") or email
        if name:
            names.append(name)
    return names


def _build_observation(doc: dict, transcript_entries: list[dict] | None = None) -> Observation | None:
    """Build one Observation from a Granola document + optional transcript."""
    title = doc.get("title") or "(untitled meeting)"
    notes_md = (doc.get("notes_markdown") or "").strip()
    notes_plain = (doc.get("notes_plain") or "").strip()
    overview = (doc.get("overview") or "").strip()
    summary = (doc.get("summary") or "").strip()

    attendees = _attendee_names(doc)
    created = _parse_iso(doc.get("created_at"))
    if not created:
        return None

    # Even meetings with zero notes/transcript are still a signal if
    # they had real attendees — "this meeting happened" is enough for
    # the wiki to move a project from "scheduled" to "met."
    has_notes = bool(notes_md or notes_plain)
    has_transcript = bool(transcript_entries)
    has_summary = bool(overview or summary)
    has_attendees = bool(attendees)
    if not has_notes and not has_transcript and not has_summary and not has_attendees:
        return None

    # Build the text digest.
    lines: list[str] = []
    if attendees:
        lines.append(f"Meeting: {title} (with {', '.join(attendees[:8])})")
    else:
        lines.append(f"Meeting: {title}")

    cal = doc.get("google_calendar_event") or {}
    cal_summary = cal.get("summary") or ""
    start = cal.get("start", {})
    cal_time = start.get("dateTime") or start.get("date") or ""
    if cal_time:
        lines.append(f"Scheduled: {cal_time[:19]}")

    # Compute meeting duration from created_at → updated_at as a hint.
    updated = _parse_iso(doc.get("updated_at"))
    if updated and created and updated > created:
        duration_min = int((updated - created).total_seconds() / 60)
        if duration_min > 1:
            lines.append(f"Duration: ~{duration_min} minutes")

    if not has_notes and not has_transcript and not has_summary:
        # No content recorded — still emit so the wiki knows the
        # meeting happened (moves "scheduled a call" → "met on <date>").
        lines.append("(No notes or transcript recorded for this meeting.)")

    if overview:
        lines.append(f"Overview: {overview[:500]}")
    if summary:
        lines.append(f"Summary: {summary[:500]}")

    # Prefer markdown notes; fall back to plain.
    notes = notes_md or notes_plain
    if notes:
        lines.append(f"Notes:\n{notes[:3000]}")

    # Append condensed transcript if available (cap to keep total
    # under 4KB so batching stays predictable).
    if transcript_entries:
        budget = 4000 - sum(len(l) for l in lines) - 50
        if budget > 200:
            transcript_lines: list[str] = ["Transcript excerpt:"]
            used = 0
            for entry in transcript_entries:
                text = (entry.get("text") or "").strip()
                if not text:
                    continue
                ts = (entry.get("start_timestamp") or "")[:19]
                source = entry.get("source", "")
                line = f"  [{ts}] ({source}): {text}"
                if used + len(line) > budget:
                    break
                transcript_lines.append(line)
                used += len(line)
            if len(transcript_lines) > 1:
                lines.extend(transcript_lines)

    digest = "\n".join(lines)[:4500]
    doc_id = doc.get("id") or title

    sender = f"Meeting: {title}"
    if attendees:
        sender = f"Meeting with {', '.join(attendees[:3])}"

    return Observation(
        source="granola",
        sender=sender[:100],
        text=digest,
        timestamp=created,
        id_key=f"granola-{doc_id}",
    )


# ---------------------------------------------------------------------------
# Steady-state collector
# ---------------------------------------------------------------------------


def collect_recent_granola(since_minutes: int = 15) -> list[Observation]:
    """Return Observations for meetings updated in the last N minutes."""
    docs = _load_documents()
    if not docs:
        return []

    transcripts = _load_transcripts()
    cutoff = datetime.now() - timedelta(minutes=since_minutes)
    results: list[Observation] = []

    for doc_id, doc in docs.items():
        updated = _parse_iso(doc.get("updated_at"))
        if not updated or updated < cutoff:
            continue
        if doc.get("deleted_at"):
            continue
        transcript = transcripts.get(doc_id) or []
        obs = _build_observation(doc, transcript if transcript else None)
        if obs:
            results.append(obs)

    return results


# ---------------------------------------------------------------------------
# Calendar enrichment — join Granola notes onto calendar events
# ---------------------------------------------------------------------------


def _granola_doc_attendee_emails(doc: dict) -> set[str]:
    """Extract all attendee emails from a Granola doc (lowercased)."""
    cal = doc.get("google_calendar_event") or {}
    attendees = cal.get("attendees") or []
    return {(a.get("email") or "").lower() for a in attendees} - {""}


def enrich_calendar_observations(
    cal_observations: list[Observation],
    days: int = 30,
) -> list[Observation]:
    """Join Granola notes onto calendar-sourced Observations.

    For each calendar observation, try to find a matching Granola doc
    (same day + overlapping attendee emails). If found and the doc has
    notes/transcript, append the Granola content to the observation's
    text. Returns the enriched list (mutated in place) plus any unmatched
    Granola docs that have content as additional standalone observations
    (meetings on other calendars, personal notes, etc.).

    This is the onboarding path. The steady-state collector handles
    Granola independently since the monitor loop's dedup handles the
    merge naturally.
    """
    docs = _load_documents()
    transcripts = _load_transcripts()
    if not docs:
        return cal_observations

    cutoff = datetime.now() - timedelta(days=days)

    # Build a lookup of Granola docs by date → list of (doc, attendee_emails)
    docs_by_date: dict[str, list[tuple[dict, set[str]]]] = {}
    for doc_id, doc in docs.items():
        if doc.get("deleted_at"):
            continue
        created = _parse_iso(doc.get("created_at"))
        if not created or created < cutoff:
            continue
        date_key = created.strftime("%Y-%m-%d")
        emails = _granola_doc_attendee_emails(doc)
        docs_by_date.setdefault(date_key, []).append((doc, emails))

    matched_doc_ids: set[str] = set()

    # Try to match each calendar observation to a Granola doc.
    for obs in cal_observations:
        date_key = obs.timestamp.strftime("%Y-%m-%d")
        candidates = docs_by_date.get(date_key, [])
        if not candidates:
            continue

        # Match by attendee email overlap in the observation text.
        # Calendar observations include "Attendees: Name (email), ..."
        obs_text_lower = obs.text.lower()

        best_match: dict | None = None
        best_overlap = 0
        for doc, doc_emails in candidates:
            if not doc_emails:
                continue
            overlap = sum(1 for e in doc_emails if e in obs_text_lower)
            if overlap > best_overlap:
                best_overlap = overlap
                best_match = doc

        if best_match is None or best_overlap == 0:
            continue

        doc_id = best_match.get("id", "")
        matched_doc_ids.add(doc_id)

        # Append Granola content to the calendar observation.
        notes_md = (best_match.get("notes_markdown") or "").strip()
        notes_plain = (best_match.get("notes_plain") or "").strip()
        overview = (best_match.get("overview") or "").strip()
        summary = (best_match.get("summary") or "").strip()
        transcript = transcripts.get(doc_id) or []

        additions: list[str] = []
        if overview:
            additions.append(f"AI Overview: {overview[:500]}")
        if summary:
            additions.append(f"AI Summary: {summary[:500]}")
        notes = notes_md or notes_plain
        if notes:
            additions.append(f"Meeting Notes:\n{notes[:3000]}")
        if transcript:
            excerpt_lines: list[str] = []
            budget = 1500
            used = 0
            for entry in transcript:
                text = (entry.get("text") or "").strip()
                if not text:
                    continue
                ts = (entry.get("start_timestamp") or "")[:19]
                line = f"  [{ts}]: {text}"
                if used + len(line) > budget:
                    break
                excerpt_lines.append(line)
                used += len(line)
            if excerpt_lines:
                additions.append("Transcript excerpt:\n" + "\n".join(excerpt_lines))

        if additions:
            enrichment = "\n\n".join(additions)
            # Extend the observation text, respecting a generous cap.
            obs.text = (obs.text + "\n\n" + enrichment)[:5000]

    # Emit unmatched Granola docs with content as standalone observations.
    # These are meetings that didn't appear on the user's calendar
    # (other people's calendar invites, ad-hoc Granola captures, etc.).
    extra: list[Observation] = []
    for doc_id, doc in docs.items():
        if doc_id in matched_doc_ids:
            continue
        if doc.get("deleted_at"):
            continue
        created = _parse_iso(doc.get("created_at"))
        if not created or created < cutoff:
            continue
        transcript = transcripts.get(doc_id) or []
        obs = _build_observation(doc, transcript if transcript else None)
        if obs:
            extra.append(obs)

    if extra:
        extra.sort(key=lambda o: o.timestamp)
        log.info(
            "Granola enrichment: matched %d calendar events, "
            "%d unmatched Granola docs emitted standalone",
            len(matched_doc_ids), len(extra),
        )

    return cal_observations + extra


# ---------------------------------------------------------------------------
# Onboarding backfill (standalone, for docs not covered by calendar)
# ---------------------------------------------------------------------------


def fetch_granola_backfill(days: int = 30) -> list[Observation]:
    """Return one Observation per Granola meeting in the last ``days`` days.

    In the new architecture this is the FALLBACK path — most meetings
    should come through ``fetch_calendar_backfill`` + Granola enrichment.
    This catches meetings that exist in Granola but not on the user's
    primary calendar (other people's invites, ad-hoc captures, etc.).
    """
    docs = _load_documents()
    if not docs:
        log.warning("Granola cache not found or empty at %s", CACHE_PATH)
        return []

    transcripts = _load_transcripts()
    cutoff = datetime.now() - timedelta(days=days)
    results: list[Observation] = []

    for doc_id, doc in docs.items():
        created = _parse_iso(doc.get("created_at"))
        if not created or created < cutoff:
            continue
        if doc.get("deleted_at"):
            continue
        transcript = transcripts.get(doc_id) or []
        obs = _build_observation(doc, transcript if transcript else None)
        if obs:
            results.append(obs)

    results.sort(key=lambda o: o.timestamp)
    log.info("Granola backfill: %d meetings with content in last %d days", len(results), days)
    return results
