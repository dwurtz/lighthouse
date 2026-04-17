"""Contact resolution — maps phone numbers and display names to real people.

Reads from TWO buffers:

  1. ``~/.deja/contacts_buffer.json`` — written by the Swift app from
     the macOS AddressBook SQLite database. Only the Swift app needs
     Contacts permission; Python never touches the database directly.
  2. ``~/.deja/google_contacts_buffer.json`` — written by
     ``observations.google_contacts.sync_google_contacts`` from the
     People API. Catches work contacts that live only in Gmail.

When the same phone number appears in both sources **macOS wins** —
the user-curated name in Contacts is the higher-signal label; Google
Contacts is often auto-populated from email headers and can be noisy
("jane@corp.com" → "Jane Corp Support").

Cache is built on first use and lives in memory only.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from deja.config import DEJA_HOME

log = logging.getLogger(__name__)

_phone_index: dict[str, str] | None = None
_name_set: set[str] | None = None

# Buffer file written by the Swift app
_CONTACTS_BUFFER = DEJA_HOME / "contacts_buffer.json"
# Buffer file written by the Google People sync
_GOOGLE_BUFFER = DEJA_HOME / "google_contacts_buffer.json"


def _normalize_phone(phone: str) -> str:
    digits = re.sub(r'\D', '', phone)
    return digits[-10:] if len(digits) > 10 else digits


def _load_buffer(path: Path) -> list[dict]:
    """Load a contacts-buffer JSON file or return []."""
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        if isinstance(data, list):
            return data
    except Exception:
        log.debug("Failed to read contacts buffer: %s", path)
    return []


def _ingest_entries(
    entries: list[dict],
    *,
    phone_index: dict[str, str],
    name_set: set[str],
    overwrite: bool,
) -> tuple[int, int]:
    """Merge one buffer's entries into the shared indexes.

    ``overwrite=True`` lets the entry's phone mapping replace any
    existing one (macOS pass). ``overwrite=False`` only fills
    phones that weren't already claimed (Google pass). Returns
    (phone_mappings_added, names_added).
    """
    phones_added = 0
    names_added = 0
    for entry in entries:
        name = (entry.get("name") or "").strip()
        if not name:
            continue
        lname = name.lower()
        if lname not in name_set:
            name_set.add(lname)
            names_added += 1

        raw_phones = entry.get("phones") or ""
        # Both buffers use comma-joined strings; but tolerate a list
        # too in case an upstream writer gets it "right" someday.
        if isinstance(raw_phones, list):
            phone_iter = raw_phones
        else:
            phone_iter = str(raw_phones).split(",")

        for phone in phone_iter:
            phone = phone.strip()
            if not phone:
                continue
            normalized = _normalize_phone(phone)
            if not normalized:
                continue
            if overwrite or normalized not in phone_index:
                phone_index[normalized] = name
                phones_added += 1
    return phones_added, names_added


def _build_index():
    """Build phone->name index from both contact buffers.

    Priority order: macOS first (user-curated, wins on conflict),
    then Google (fills in gaps). We log how many mappings each
    source contributed so drift is visible in the startup log.
    """
    global _phone_index, _name_set
    _phone_index = {}
    _name_set = set()

    macos_entries = _load_buffer(_CONTACTS_BUFFER)
    google_entries = _load_buffer(_GOOGLE_BUFFER)

    # Pass 1: macOS. overwrite=True is moot on a fresh dict but
    # makes the priority rule explicit for future refactors.
    macos_phones, macos_names = _ingest_entries(
        macos_entries,
        phone_index=_phone_index,
        name_set=_name_set,
        overwrite=True,
    )
    # Pass 2: Google. overwrite=False so macOS numbers stick.
    google_phones, google_names = _ingest_entries(
        google_entries,
        phone_index=_phone_index,
        name_set=_name_set,
        overwrite=False,
    )

    log.info(
        "Loaded %d contacts, %d phone mappings "
        "(macos: %d contacts/%d phones; google: %d contacts/%d phones)",
        len(_name_set), len(_phone_index),
        len(macos_entries), macos_phones,
        len(google_entries), google_phones,
    )
    if google_names:
        log.debug("Google added %d new names beyond macOS", google_names)


def name_with_handle(name: str, handle: str) -> str:
    """Render 'Jane Doe (+15551234567)', or just the handle if name and
    handle are the same string (i.e. contact resolution failed)."""
    if name == handle or not handle:
        return name
    return f"{name} ({handle})"


def resolve_contact(identifier: str) -> str | None:
    """Resolve a phone number or display name to a contact name."""
    if _phone_index is None:
        _build_index()

    normalized = _normalize_phone(identifier)
    if normalized and _phone_index and normalized in _phone_index:
        return _phone_index[normalized]

    return None


def get_contacts_summary() -> str:
    """Get a summary of contacts who are key people in goals.
    Only includes goal-relevant contacts, not all 914."""
    # Don't dump all contacts — just return a note that contacts are available
    if _phone_index is None:
        _build_index()
    return f"({len(_name_set or [])} contacts available for name resolution)"
