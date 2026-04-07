"""Contact resolution — maps phone numbers and display names to real people.

Reads from ~/.deja/contacts_buffer.json, which is written by the Swift app
from the macOS AddressBook SQLite database. This way only the Swift app
needs Contacts permission; Python never touches the database directly.

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


def _normalize_phone(phone: str) -> str:
    digits = re.sub(r'\D', '', phone)
    return digits[-10:] if len(digits) > 10 else digits


def _build_index():
    """Build phone->name index from the contacts buffer JSON."""
    global _phone_index, _name_set
    _phone_index = {}
    _name_set = set()

    if not _CONTACTS_BUFFER.exists():
        log.debug("Contacts buffer not found at %s — skipping", _CONTACTS_BUFFER)
        return

    try:
        data = json.loads(_CONTACTS_BUFFER.read_text())
    except Exception:
        log.debug("Failed to read contacts buffer: %s", _CONTACTS_BUFFER)
        return

    for entry in data:
        name = (entry.get("name") or "").strip()
        if not name:
            continue
        _name_set.add(name.lower())
        for phone in (entry.get("phones") or "").split(","):
            phone = phone.strip()
            if phone:
                normalized = _normalize_phone(phone)
                if normalized:
                    _phone_index[normalized] = name

    log.info("Loaded %d contacts, %d phone mappings", len(_name_set), len(_phone_index))


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
    return f"({len(_name_set or [])} macOS contacts available for name resolution)"
