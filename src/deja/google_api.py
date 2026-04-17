"""Thin wrapper around ``googleapiclient`` that reuses Deja's OAuth token.

Why this exists
---------------

Deja historically shelled out to the ``gws`` CLI (a developer tool) for
every Gmail / Calendar / Tasks call. That's fine in dev but a blocker
for distribution: end users won't have ``gws`` installed. This module
replaces every ``subprocess.run(["gws", ...])`` call site with a direct
``googleapiclient.discovery.build(...)`` service, authenticated off the
same OAuth token we already collect at setup.

Token source
------------

Credentials are loaded through ``deja.auth`` — which transparently
reads from (in priority order):

  1. macOS Keychain ``service=deja / account=google-token``
  2. ``~/.deja/google_token.json``
  3. ``~/.config/gws/token.json`` (legacy migration)

…and refreshes the access token if it's expired. We rebuild the
``google.oauth2.credentials.Credentials`` object from that payload and
hand it to ``googleapiclient``. If the token doesn't exist we raise
:class:`deja.observability.errors.AuthError` — callers are expected to
have completed setup before reaching this module.

Caching
-------

Discovery is slow (HTTP roundtrip to fetch the API schema) so we keep a
process-wide cache keyed by ``(name, version)``. Credentials are mutated
in-place by ``googleapiclient`` when the access token refreshes mid-run,
so one cached service per pair is fine — the underlying creds keep
themselves fresh.

Error translation
-----------------

``googleapiclient.errors.HttpError`` is the analog of the gws non-zero
exit. Call sites should translate it into the appropriate
``DejaError`` subclass (``AuthError`` for 401/403, plain log-and-skip
for everything else) so upstream catchers still work.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

from deja.observability.errors import AuthError

log = logging.getLogger(__name__)

# Process-wide service cache. googleapiclient services are thread-safe
# for read-only use — they don't hold a connection, they build HTTP
# requests on demand. Lock the build step only.
_service_cache: dict[tuple[str, str], Any] = {}
_cache_lock = threading.Lock()


def _load_credentials():
    """Build a ``google.oauth2.credentials.Credentials`` from Deja's token.

    Uses ``deja.auth._read_token_file`` so the Keychain + migration
    paths stay in one place. Never caches the Credentials object across
    calls — auth.py already refreshes expired tokens and persists them,
    so a fresh Credentials wrapping the current disk state is correct.

    Raises :class:`AuthError` if no token is present (user hasn't run
    setup) or if it lacks a refresh token (unrecoverable).
    """
    # Lazy import — auth.py imports lots of OAuth stuff we don't want
    # to pay for at module-load time.
    from deja import auth
    from google.oauth2.credentials import Credentials

    # Trigger refresh-if-expired by calling get_access_token(); that
    # rewrites the token file so the subsequent _read_token_file sees
    # the fresh blob. If refresh failed, we get None back and fail
    # fast with a typed error.
    fresh_access = auth.get_access_token()
    if not fresh_access:
        raise AuthError(
            "Google OAuth token missing or unrecoverable",
            user_message=(
                "Deja isn't signed in to Google. Open the app and "
                "complete setup to reconnect."
            ),
        )

    token_data = auth._read_token_file()
    if not token_data:
        raise AuthError("Google OAuth token disappeared after refresh")

    return Credentials(
        token=token_data.get("access_token") or token_data.get("token"),
        refresh_token=token_data.get("refresh_token"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=token_data.get("client_id"),
        client_secret=token_data.get("client_secret"),
        scopes=token_data.get("scopes") or auth.SCOPES,
    )


def get_service(name: str, version: str):
    """Return a cached ``googleapiclient`` service for ``(name, version)``.

    Example::

        svc = get_service("gmail", "v1")
        profile = svc.users().getProfile(userId="me").execute()

    Thread-safe. The returned service can be freely shared across
    threads — googleapiclient builds a new request per ``.execute()``.

    Raises :class:`AuthError` if setup hasn't happened.
    """
    key = (name, version)
    cached = _service_cache.get(key)
    if cached is not None:
        return cached

    with _cache_lock:
        # Re-check under the lock in case another thread beat us here.
        cached = _service_cache.get(key)
        if cached is not None:
            return cached

        from googleapiclient.discovery import build

        creds = _load_credentials()
        # cache_discovery=False: googleapiclient tries to stash the
        # schema in a process-local file under a temp dir, which
        # floods our logs with benign "file_cache is only supported
        # with oauth2client<4.0.0" noise. We don't need that cache —
        # our own _service_cache is the layer that matters.
        svc = build(
            name,
            version,
            credentials=creds,
            cache_discovery=False,
        )
        _service_cache[key] = svc
        return svc


def clear_cache() -> None:
    """Drop every cached service. Tests call this; production doesn't."""
    with _cache_lock:
        _service_cache.clear()


__all__ = ["get_service", "clear_cache"]
