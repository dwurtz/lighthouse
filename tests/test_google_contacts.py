"""Tests for the Google People sync module.

Mocks ``deja.google_api.get_service`` so we don't hit the network —
the tests focus on paging, shape-flattening, and buffer write.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


class _FakeRequest:
    def __init__(self, response):
        self._response = response

    def execute(self):
        return self._response


class _FakeConnections:
    """Mimics ``svc.people().connections().list(...)``."""

    def __init__(self, pages):
        # ``pages`` is a list of response dicts in paging order
        self._pages = list(pages)
        self._calls: list[dict] = []

    def list(self, **kwargs):
        self._calls.append(kwargs)
        # Advance to the page matching the incoming pageToken (if any).
        # Simple impl: pop the first response.
        if not self._pages:
            return _FakeRequest({"connections": []})
        return _FakeRequest(self._pages.pop(0))


class _FakePeople:
    def __init__(self, connections):
        self._connections = connections

    def connections(self):
        return self._connections


class _FakeService:
    def __init__(self, connections):
        self._people = _FakePeople(connections)

    def people(self):
        return self._people


def _make_service(pages):
    return _FakeService(_FakeConnections(pages))


def test_sync_writes_buffer(isolated_home, monkeypatch):
    """One page of connections is flattened + written to the buffer."""
    home, _ = isolated_home
    from deja.observations import google_contacts

    # Patch the buffer path onto the freshly isolated home. The module
    # captured DEJA_HOME at import time.
    buffer_path = home / "google_contacts_buffer.json"
    monkeypatch.setattr(google_contacts, "BUFFER_PATH", buffer_path)

    page_response = {
        "connections": [
            {
                "names": [{"displayName": "Jane Doe"}],
                "emailAddresses": [{"value": "jane@example.com"}],
                "phoneNumbers": [
                    {"value": "+14155551234"},
                    {"value": "+14155559999"},
                ],
                "nicknames": [{"value": "Janey"}],
            },
            {
                "names": [{"displayName": "Bob"}],
                "emailAddresses": [{"value": "bob@corp.io"}],
            },
            # No-name records are skipped
            {"emailAddresses": [{"value": "lost@example.com"}]},
        ],
    }
    svc = _make_service([page_response])

    monkeypatch.setattr(
        google_contacts,
        "get_service",
        lambda name, version: svc,
        raising=False,
    )
    # get_service is imported inside the function — patch at the source.
    from deja import google_api
    monkeypatch.setattr(google_api, "get_service", lambda n, v: svc)

    count = google_contacts.sync_google_contacts()
    assert count == 2

    records = json.loads(buffer_path.read_text())
    assert len(records) == 2
    jane = records[0]
    assert jane["name"] == "Jane Doe"
    assert jane["emails"] == ["jane@example.com"]
    assert "+14155551234" in jane["phones"]
    assert "+14155559999" in jane["phones"]
    assert jane["nicknames"] == ["Janey"]

    bob = records[1]
    assert bob["name"] == "Bob"
    assert bob["emails"] == ["bob@corp.io"]
    assert bob["phones"] == ""
    assert bob["nicknames"] == []


def test_sync_handles_pagination(isolated_home, monkeypatch):
    """Multi-page responses are all drained before write."""
    home, _ = isolated_home
    from deja.observations import google_contacts
    from deja import google_api

    buffer_path = home / "google_contacts_buffer.json"
    monkeypatch.setattr(google_contacts, "BUFFER_PATH", buffer_path)

    page1 = {
        "connections": [
            {"names": [{"displayName": "A"}]},
            {"names": [{"displayName": "B"}]},
        ],
        "nextPageToken": "tok2",
    }
    page2 = {
        "connections": [
            {"names": [{"displayName": "C"}]},
        ],
    }
    svc = _make_service([page1, page2])
    monkeypatch.setattr(google_api, "get_service", lambda n, v: svc)

    count = google_contacts.sync_google_contacts()
    assert count == 3
    names = {r["name"] for r in json.loads(buffer_path.read_text())}
    assert names == {"A", "B", "C"}


def test_sync_returns_zero_on_service_failure(isolated_home, monkeypatch):
    from deja.observations import google_contacts
    from deja import google_api

    def boom(n, v):
        raise RuntimeError("no auth")

    monkeypatch.setattr(google_api, "get_service", boom)

    assert google_contacts.sync_google_contacts() == 0


def test_sync_if_stale_skips_when_fresh(isolated_home, monkeypatch):
    """A fresh buffer means no sync — returns -1."""
    home, _ = isolated_home
    from deja.observations import google_contacts

    buffer_path = home / "google_contacts_buffer.json"
    buffer_path.write_text("[]")
    monkeypatch.setattr(google_contacts, "BUFFER_PATH", buffer_path)

    def should_not_call():
        raise AssertionError("sync should not have been called")

    monkeypatch.setattr(google_contacts, "sync_google_contacts", should_not_call)

    result = google_contacts.sync_if_stale(max_age=999_999)
    assert result == -1


def test_sync_if_stale_runs_when_missing(isolated_home, monkeypatch):
    """No buffer at all means age is infinite → run sync."""
    home, _ = isolated_home
    from deja.observations import google_contacts

    buffer_path = home / "google_contacts_buffer.json"
    monkeypatch.setattr(google_contacts, "BUFFER_PATH", buffer_path)

    called = {"n": 0}

    def fake_sync():
        called["n"] += 1
        return 5

    monkeypatch.setattr(google_contacts, "sync_google_contacts", fake_sync)

    result = google_contacts.sync_if_stale()
    assert result == 5
    assert called["n"] == 1


def test_sync_if_stale_runs_when_stale(isolated_home, monkeypatch):
    """Old buffer triggers a sync."""
    home, _ = isolated_home
    from deja.observations import google_contacts

    buffer_path = home / "google_contacts_buffer.json"
    buffer_path.write_text("[]")
    # Back-date the file to 2 days ago
    old = buffer_path.stat().st_mtime - (48 * 3600)
    os.utime(buffer_path, (old, old))
    monkeypatch.setattr(google_contacts, "BUFFER_PATH", buffer_path)

    called = {"n": 0}

    def fake_sync():
        called["n"] += 1
        return 7

    monkeypatch.setattr(google_contacts, "sync_google_contacts", fake_sync)

    result = google_contacts.sync_if_stale()
    assert result == 7
