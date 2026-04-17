"""Smoke tests for the ``deja.google_api`` thin helper.

These don't hit the network — we monkeypatch ``auth.get_access_token``
and ``googleapiclient.discovery.build`` so the assertions focus on
wiring: do we read the token correctly, do we build a Credentials
object with the right fields, do we cache the service, and do we
raise ``AuthError`` when the token is missing.
"""

from __future__ import annotations

import pytest

from deja.observability.errors import AuthError


@pytest.fixture(autouse=True)
def _clear_service_cache():
    """Every test starts with a clean service cache."""
    from deja import google_api
    google_api.clear_cache()
    yield
    google_api.clear_cache()


def test_get_service_raises_authError_when_no_token(monkeypatch):
    """If setup hasn't run we surface AuthError, not a generic crash."""
    from deja import auth, google_api

    monkeypatch.setattr(auth, "get_access_token", lambda: None)

    with pytest.raises(AuthError):
        google_api.get_service("gmail", "v1")


def test_get_service_builds_with_credentials(monkeypatch):
    """A valid token produces a Credentials object passed to build()."""
    from deja import auth, google_api
    from google.oauth2.credentials import Credentials

    token_data = {
        "access_token": "tok123",
        "refresh_token": "r-tok",
        "client_id": "cid",
        "client_secret": "secret",
        "scopes": ["https://www.googleapis.com/auth/gmail.modify"],
    }
    monkeypatch.setattr(auth, "get_access_token", lambda: "tok123")
    monkeypatch.setattr(auth, "_read_token_file", lambda: token_data)

    captured = {}

    def fake_build(name, version, credentials=None, cache_discovery=None):
        captured["name"] = name
        captured["version"] = version
        captured["credentials"] = credentials
        return f"service:{name}:{version}"

    # build lives in googleapiclient.discovery; google_api imports it
    # inside the function, so patching the module attribute works.
    import googleapiclient.discovery as discovery
    monkeypatch.setattr(discovery, "build", fake_build)

    svc = google_api.get_service("gmail", "v1")
    assert svc == "service:gmail:v1"
    assert captured["name"] == "gmail"
    assert captured["version"] == "v1"
    assert isinstance(captured["credentials"], Credentials)
    assert captured["credentials"].token == "tok123"
    assert captured["credentials"].refresh_token == "r-tok"
    assert captured["credentials"].client_id == "cid"


def test_get_service_is_cached(monkeypatch):
    """Second call with the same (name, version) returns the cached svc."""
    from deja import auth, google_api

    monkeypatch.setattr(auth, "get_access_token", lambda: "tok")
    monkeypatch.setattr(auth, "_read_token_file", lambda: {
        "access_token": "tok",
        "refresh_token": "r",
        "client_id": "c",
        "client_secret": "s",
        "scopes": [],
    })

    call_count = {"n": 0}

    def fake_build(name, version, credentials=None, cache_discovery=None):
        call_count["n"] += 1
        return object()  # unique each call

    import googleapiclient.discovery as discovery
    monkeypatch.setattr(discovery, "build", fake_build)

    svc1 = google_api.get_service("calendar", "v3")
    svc2 = google_api.get_service("calendar", "v3")
    assert svc1 is svc2
    assert call_count["n"] == 1


def test_different_services_build_separately(monkeypatch):
    from deja import auth, google_api

    monkeypatch.setattr(auth, "get_access_token", lambda: "tok")
    monkeypatch.setattr(auth, "_read_token_file", lambda: {
        "access_token": "tok",
        "refresh_token": "r",
        "client_id": "c",
        "client_secret": "s",
        "scopes": [],
    })

    call_count = {"n": 0}

    def fake_build(name, version, credentials=None, cache_discovery=None):
        call_count["n"] += 1
        return (name, version)

    import googleapiclient.discovery as discovery
    monkeypatch.setattr(discovery, "build", fake_build)

    a = google_api.get_service("gmail", "v1")
    b = google_api.get_service("calendar", "v3")
    assert a != b
    assert call_count["n"] == 2
