"""Tests for contact-index building with macOS + Google dual sourcing.

The safety-critical rule these tests pin: **macOS wins on conflict.**
User-curated names beat auto-populated Google Contacts entries that
often come from stale email headers.
"""

from __future__ import annotations

import json

import pytest


def _reset_module(monkeypatch):
    """Fresh _phone_index / _name_set for every test."""
    from deja.observations import contacts
    monkeypatch.setattr(contacts, "_phone_index", None)
    monkeypatch.setattr(contacts, "_name_set", None)
    return contacts


def test_index_loads_macos_only(isolated_home, monkeypatch):
    home, _ = isolated_home
    contacts = _reset_module(monkeypatch)
    monkeypatch.setattr(contacts, "_CONTACTS_BUFFER", home / "contacts_buffer.json")
    monkeypatch.setattr(contacts, "_GOOGLE_BUFFER", home / "google_contacts_buffer.json")

    (home / "contacts_buffer.json").write_text(json.dumps([
        {"name": "Jane Doe", "phones": "+1 415 555 1234"},
    ]))

    assert contacts.resolve_contact("+14155551234") == "Jane Doe"


def test_index_loads_google_only(isolated_home, monkeypatch):
    home, _ = isolated_home
    contacts = _reset_module(monkeypatch)
    monkeypatch.setattr(contacts, "_CONTACTS_BUFFER", home / "contacts_buffer.json")
    monkeypatch.setattr(contacts, "_GOOGLE_BUFFER", home / "google_contacts_buffer.json")

    (home / "google_contacts_buffer.json").write_text(json.dumps([
        {"name": "Bob Google", "phones": "+12125559999"},
    ]))

    assert contacts.resolve_contact("+12125559999") == "Bob Google"


def test_macos_wins_on_phone_conflict(isolated_home, monkeypatch):
    """Same number in both buffers — macOS's name is kept."""
    home, _ = isolated_home
    contacts = _reset_module(monkeypatch)
    monkeypatch.setattr(contacts, "_CONTACTS_BUFFER", home / "contacts_buffer.json")
    monkeypatch.setattr(contacts, "_GOOGLE_BUFFER", home / "google_contacts_buffer.json")

    (home / "contacts_buffer.json").write_text(json.dumps([
        {"name": "Coach Rob", "phones": "+14155551234"},
    ]))
    (home / "google_contacts_buffer.json").write_text(json.dumps([
        # Google autocaptured the number as Rob's work email display name
        {"name": "Robert Toyson (Acme Corp)", "phones": "+14155551234"},
    ]))

    # macOS's curated name wins
    assert contacts.resolve_contact("+14155551234") == "Coach Rob"


def test_both_sources_merge_names(isolated_home, monkeypatch):
    """Distinct people from each source both end up in the name set."""
    home, _ = isolated_home
    contacts = _reset_module(monkeypatch)
    monkeypatch.setattr(contacts, "_CONTACTS_BUFFER", home / "contacts_buffer.json")
    monkeypatch.setattr(contacts, "_GOOGLE_BUFFER", home / "google_contacts_buffer.json")

    (home / "contacts_buffer.json").write_text(json.dumps([
        {"name": "Jane Doe", "phones": "+14155551111"},
    ]))
    (home / "google_contacts_buffer.json").write_text(json.dumps([
        {"name": "Bob Google", "phones": "+14155552222"},
    ]))

    # Both phones resolve correctly
    assert contacts.resolve_contact("+14155551111") == "Jane Doe"
    assert contacts.resolve_contact("+14155552222") == "Bob Google"
    # Both names are registered
    summary = contacts.get_contacts_summary()
    assert "2 contacts" in summary


def test_missing_buffers_degrade_gracefully(isolated_home, monkeypatch):
    """No buffer files at all → empty index, no crash."""
    home, _ = isolated_home
    contacts = _reset_module(monkeypatch)
    monkeypatch.setattr(contacts, "_CONTACTS_BUFFER", home / "contacts_buffer.json")
    monkeypatch.setattr(contacts, "_GOOGLE_BUFFER", home / "google_contacts_buffer.json")

    # No files exist
    assert contacts.resolve_contact("+14155551234") is None


def test_google_buffer_with_list_phones(isolated_home, monkeypatch):
    """Google buffer's phones can also be a list (future-proof)."""
    home, _ = isolated_home
    contacts = _reset_module(monkeypatch)
    monkeypatch.setattr(contacts, "_CONTACTS_BUFFER", home / "contacts_buffer.json")
    monkeypatch.setattr(contacts, "_GOOGLE_BUFFER", home / "google_contacts_buffer.json")

    (home / "google_contacts_buffer.json").write_text(json.dumps([
        {"name": "Alice", "phones": ["+14155551234", "+14155559999"]},
    ]))

    assert contacts.resolve_contact("+14155551234") == "Alice"
    assert contacts.resolve_contact("+14155559999") == "Alice"


def test_name_with_handle_unchanged():
    """name_with_handle is unaffected by the merge refactor."""
    from deja.observations.contacts import name_with_handle
    assert name_with_handle("Jane", "+15551234567") == "Jane (+15551234567)"
    assert name_with_handle("Jane", "Jane") == "Jane"
    assert name_with_handle("Jane", "") == "Jane"
