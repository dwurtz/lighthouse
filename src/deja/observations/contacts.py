"""Contact resolution — maps phone numbers and display names to real people.

Reads directly from macOS AddressBook SQLite database. No local contacts.json needed.
Cache is built on first use and lives in memory only.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path

log = logging.getLogger(__name__)

_phone_index: dict[str, str] | None = None
_name_set: set[str] | None = None


def _normalize_phone(phone: str) -> str:
    digits = re.sub(r'\D', '', phone)
    return digits[-10:] if len(digits) > 10 else digits


def _build_index():
    """Build phone→name index from macOS AddressBook SQLite."""
    global _phone_index, _name_set
    _phone_index = {}
    _name_set = set()

    ab_dir = Path.home() / "Library" / "Application Support" / "AddressBook" / "Sources"
    if not ab_dir.exists():
        return

    for db_path in ab_dir.glob("*/AddressBook-v22.abcddb"):
        try:
            conn = sqlite3.connect(str(db_path))
            rows = conn.execute("""
                SELECT
                    COALESCE(r.ZFIRSTNAME, '') || ' ' || COALESCE(r.ZLASTNAME, '') as name,
                    GROUP_CONCAT(DISTINCT p.ZFULLNUMBER) as phones
                FROM ZABCDRECORD r
                LEFT JOIN ZABCDPHONENUMBER p ON p.ZOWNER = r.Z_PK
                WHERE r.ZFIRSTNAME IS NOT NULL
                GROUP BY r.Z_PK
            """).fetchall()
            conn.close()

            for name, phones_str in rows:
                name = name.strip()
                if not name:
                    continue
                _name_set.add(name.lower())
                for phone in (phones_str or "").split(","):
                    phone = phone.strip()
                    if phone:
                        normalized = _normalize_phone(phone)
                        if normalized:
                            _phone_index[normalized] = name
        except Exception:
            log.debug("Failed to read AddressBook DB: %s", db_path)

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
