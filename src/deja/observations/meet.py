"""Google Meet transcript collector.

Meet transcripts auto-save to Google Drive as Google Docs. This
collector queries Drive for transcript documents and reads their
content via the Docs API — both through the ``gws`` CLI that's
already authenticated on the user's machine.

Two entry points:

  * ``collect_recent_transcripts`` — steady-state, called every few
    cycles by the monitor. Returns transcripts modified in the last N
    minutes.
  * ``fetch_meet_transcripts_backfill`` — onboarding path. Returns
    one Observation per transcript document in the last N days.
"""

from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime, timedelta, timezone

from deja.observations.base import BaseObserver
from deja.observations.types import Observation

log = logging.getLogger(__name__)


def _run_gws(*args: str, timeout: int = 30) -> dict | None:
    """Run a gws command and return parsed JSON, or None on failure."""
    cmd = ["gws"] + list(args)
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            log.warning("gws command failed: %s — %s", " ".join(cmd), result.stderr[:200])
            return None
        return json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        log.warning("gws command timed out: %s", " ".join(cmd))
        return None
    except json.JSONDecodeError:
        log.warning("gws returned invalid JSON: %s", " ".join(cmd))
        return None
    except FileNotFoundError:
        log.warning("gws CLI not found on PATH")
        return None


def _list_transcript_docs(
    after: datetime | None = None,
    max_results: int = 50,
) -> list[dict]:
    """List Google Drive files that look like Meet transcripts.

    Meet names them: ``<Meeting Title> (<Date>) - Transcript``
    They have mimeType ``application/vnd.google-apps.document``.
    """
    q_parts = [
        "name contains 'Transcript'",
        "mimeType='application/vnd.google-apps.document'",
        "trashed=false",
    ]
    if after:
        iso = after.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        q_parts.append(f"modifiedTime > '{iso}'")

    query = " and ".join(q_parts)
    params = {
        "q": query,
        "pageSize": max_results,
        "fields": "files(id,name,modifiedTime,createdTime)",
        "orderBy": "modifiedTime desc",
    }
    data = _run_gws(
        "drive", "files", "list",
        "--params", json.dumps(params),
        "--format", "json",
    )
    if data is None:
        return []
    return data.get("files", []) or []


def _read_doc_text(doc_id: str) -> str:
    """Read a Google Doc's text content via the Docs API.

    Returns the plain text body, or "" on failure. The Docs API returns
    structured JSON (paragraphs, elements, etc.) — we flatten it to
    plain text since the LLM doesn't need the formatting.
    """
    data = _run_gws(
        "docs", "documents", "get",
        "--params", json.dumps({"documentId": doc_id}),
        "--format", "json",
        timeout=15,
    )
    if data is None:
        return ""

    # Walk the Docs body → content → paragraph → elements → textRun → content
    body = data.get("body", {})
    content_elements = body.get("content", [])
    text_parts: list[str] = []
    for block in content_elements:
        paragraph = block.get("paragraph")
        if not paragraph:
            continue
        for element in paragraph.get("elements", []):
            text_run = element.get("textRun")
            if text_run:
                text_parts.append(text_run.get("content", ""))
    return "".join(text_parts).strip()


def _parse_drive_time(ts: str | None) -> datetime | None:
    """Parse a Drive API timestamp (RFC 3339) to naive local datetime."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.astimezone().replace(tzinfo=None)
    except (ValueError, TypeError):
        return None


def _build_observation(file_meta: dict, doc_text: str) -> Observation | None:
    """Build one Observation from a Drive file entry + its doc content."""
    if not doc_text.strip():
        return None

    name = file_meta.get("name", "(untitled)")
    doc_id = file_meta.get("id", "")
    created = _parse_drive_time(
        file_meta.get("createdTime") or file_meta.get("modifiedTime")
    )
    if not created:
        return None

    # Parse meeting title from the transcript name.
    # Pattern: "<Title> (<Date>) - Transcript"
    title = name
    if " - Transcript" in title:
        title = title.split(" - Transcript")[0].strip()

    # Cap transcript text to ~4KB for batching.
    text = f"Meeting transcript: {title}\n\n{doc_text[:4000]}"

    return Observation(
        source="meet_transcript",
        sender=f"Meeting: {title}",
        text=text,
        timestamp=created,
        id_key=f"meet-transcript-{doc_id}",
    )


# ---------------------------------------------------------------------------
# Steady-state collector
# ---------------------------------------------------------------------------


class MeetObserver(BaseObserver):
    """Collects recent Google Meet transcripts from Drive."""

    def __init__(self, since_minutes: int = 15) -> None:
        self.since_minutes = since_minutes

    @property
    def name(self) -> str:
        return "Meet Transcripts"

    def collect(self) -> list[Observation]:
        return collect_recent_transcripts(since_minutes=self.since_minutes)


def collect_recent_transcripts(since_minutes: int = 15) -> list[Observation]:
    """Return Observations for Meet transcripts modified in the last N min."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=since_minutes)
    files = _list_transcript_docs(after=cutoff, max_results=10)
    if not files:
        return []

    results: list[Observation] = []
    for f in files:
        doc_text = _read_doc_text(f.get("id", ""))
        obs = _build_observation(f, doc_text)
        if obs:
            results.append(obs)

    return results


# ---------------------------------------------------------------------------
# Onboarding backfill
# ---------------------------------------------------------------------------


def fetch_meet_transcripts_backfill(days: int = 30) -> list[Observation]:
    """Return one Observation per Meet transcript doc in the last ``days`` days.

    Queries Drive for docs named ``*Transcript*`` and reads each one
    via the Docs API. Rate-limited by the sequential subprocess calls
    (~2–5s per doc), so 30 transcripts takes ~1–2 minutes. Fine for a
    one-time backfill.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    files = _list_transcript_docs(after=cutoff, max_results=200)
    if not files:
        log.info("Meet transcript backfill: no transcript docs found in last %d days", days)
        return []

    log.info("Meet transcript backfill: found %d transcript docs, reading content", len(files))
    results: list[Observation] = []
    for f in files:
        doc_text = _read_doc_text(f.get("id", ""))
        obs = _build_observation(f, doc_text)
        if obs:
            results.append(obs)

    results.sort(key=lambda o: o.timestamp)
    log.info("Meet transcript backfill: returning %d transcripts", len(results))
    return results
