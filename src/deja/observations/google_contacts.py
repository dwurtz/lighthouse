"""Google People API contact sync — second source for entity resolution.

Why this exists
---------------

``observations/contacts.py`` resolves phone numbers and display names
against the user's macOS AddressBook. That's great for users who curate
Contacts — but plenty of professional contacts live only in Gmail /
Google Contacts. Without this module, email-only contacts show up as
unknown handles and the wiki writer can't link them.

Sync cadence
------------

- On import of this module? No — explicit call only.
- The agent's observation cycle calls ``sync_if_stale()`` on every
  tick (see ``agent/observation_cycle.py``). That call is a cheap
  ``mtime`` check — it only hits the People API when the buffer is
  older than ``STALE_SECONDS`` (24h). This keeps the contact index
  current in long-running sessions without depending on an app
  restart.

Buffer shape
------------

Matches ``~/.deja/contacts_buffer.json`` (the macOS one) as closely as
possible so ``observations.contacts._build_index`` can merge both with
minimal branching:

    [
        {
            "name": "Jane Doe",
            "phones": "+14155551234,+14155559999",   # comma-joined like macOS
            "emails": ["jane@example.com", "jane@work.com"],
            "nicknames": ["Janey"]
        },
        ...
    ]

The macOS collector writes ``phones`` as a comma-separated string
(legacy Swift shape). We match that exactly here.

Auth
----

Uses ``deja.google_api.get_service("people", "v1")`` — same OAuth token
Deja already collected. The ``contacts`` scope is already requested in
``deja.auth.SCOPES``, so no re-consent is needed.
"""

from __future__ import annotations

import json
import logging
import tempfile
import time
from pathlib import Path

from deja.config import DEJA_HOME

log = logging.getLogger(__name__)

BUFFER_PATH = DEJA_HOME / "google_contacts_buffer.json"

# Refresh the buffer if it's older than a day. Contacts don't change
# often; daily sync is well below any rate limit and keeps ingest fresh
# for new email correspondents the user just added.
STALE_SECONDS = 24 * 60 * 60

_PAGE_SIZE = 1000  # Max per Google People API docs
# Hard cap on pagination so a pathological response can't loop forever.
_MAX_PAGES = 50


def _write_buffer_atomic(records: list[dict]) -> None:
    """Atomic write to avoid partial reads by the contacts indexer."""
    DEJA_HOME.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".google_contacts.", dir=str(DEJA_HOME))
    try:
        import os
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False)
        os.replace(tmp, BUFFER_PATH)
    except Exception:
        try:
            import os
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _flatten_connection(person: dict) -> dict | None:
    """Project a Google People connection into the contacts-buffer shape.

    ``names`` / ``emailAddresses`` / ``phoneNumbers`` / ``nicknames``
    each come back as lists of dicts with ``value`` plus a ``metadata``
    blob marking primary vs secondary. We keep every value — the index
    builder in ``contacts.py`` takes whatever we give it.

    Returns None for records with no usable name — we can't resolve
    anonymous numbers against them.
    """
    names = person.get("names") or []
    primary_name = ""
    if names:
        # The first entry is typically the primary (metadata.primary=True).
        primary_name = (names[0].get("displayName") or "").strip()
    if not primary_name:
        return None

    emails = [
        e.get("value", "").strip()
        for e in (person.get("emailAddresses") or [])
        if e.get("value")
    ]
    phones = [
        p.get("value", "").strip()
        for p in (person.get("phoneNumbers") or [])
        if p.get("value")
    ]
    nicknames = [
        n.get("value", "").strip()
        for n in (person.get("nicknames") or [])
        if n.get("value")
    ]

    return {
        "name": primary_name,
        # Match the macOS buffer shape: comma-joined phone string.
        # _build_index in observations.contacts splits on ",".
        "phones": ",".join(phones),
        "emails": emails,
        "nicknames": nicknames,
    }


def sync_google_contacts() -> int:
    """Fetch every Google contact and overwrite the buffer.

    Returns the number of records written, or 0 on failure. Failure
    is logged and swallowed — this is a best-effort enrichment, not
    a load-bearing signal path.
    """
    try:
        from deja.google_api import get_service
        svc = get_service("people", "v1")
    except Exception:
        log.warning(
            "Google People sync skipped — service unavailable",
            exc_info=True,
        )
        return 0

    records: list[dict] = []
    page_token: str | None = None

    for _ in range(_MAX_PAGES):
        try:
            req = svc.people().connections().list(
                resourceName="people/me",
                pageSize=_PAGE_SIZE,
                personFields="names,phoneNumbers,emailAddresses,nicknames",
                pageToken=page_token,
            )
            data = req.execute()
        except Exception as e:
            log.warning(
                "Google People sync failed: %s",
                type(e).__name__,
            )
            return 0

        for person in data.get("connections", []) or []:
            flat = _flatten_connection(person)
            if flat:
                records.append(flat)

        page_token = data.get("nextPageToken")
        if not page_token:
            break

    try:
        _write_buffer_atomic(records)
    except Exception:
        log.exception("Failed to write Google contacts buffer")
        return 0

    log.info("Google People sync: %d contacts written to %s",
             len(records), BUFFER_PATH)
    return len(records)


def _buffer_age_seconds() -> float:
    """How old the buffer is; returns infinity if missing."""
    try:
        return time.time() - BUFFER_PATH.stat().st_mtime
    except FileNotFoundError:
        return float("inf")


def sync_if_stale(max_age: float = STALE_SECONDS) -> int:
    """Refresh the buffer if it's missing or older than ``max_age`` seconds.

    Returns the number of records synced, or -1 if no sync was needed.
    """
    age = _buffer_age_seconds()
    if age <= max_age:
        log.debug(
            "Google contacts buffer fresh (age=%.0fs, threshold=%.0fs)",
            age, max_age,
        )
        return -1
    return sync_google_contacts()


__all__ = [
    "BUFFER_PATH",
    "STALE_SECONDS",
    "sync_google_contacts",
    "sync_if_stale",
]
