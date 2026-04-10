"""Server-driven feature flags.

Fetches /v1/config from the Deja API server, extracts feature_flags,
and caches them to ~/.deja/feature_flags.json. Other modules read from
the cache (synchronous, no network call) so flags are available immediately.

The flags refresh on app startup. To force a refresh mid-session, call
sync_feature_flags() directly.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

_FLAGS_PATH = Path.home() / ".deja" / "feature_flags.json"


def sync_feature_flags() -> dict:
    """Fetch feature flags from the server and write to the local cache.

    Returns the fetched flags dict (or {} on failure). Never raises —
    failures are logged and the cache is left untouched.
    """
    try:
        import httpx
        from deja.auth import get_auth_token
        from deja.llm_client import DEJA_API_URL

        token = get_auth_token()
        headers = {"Authorization": f"Bearer {token}"} if token else {}

        with httpx.Client(timeout=5) as client:
            resp = client.get(f"{DEJA_API_URL}/v1/config", headers=headers)
            resp.raise_for_status()
            data = resp.json()

        flags = data.get("feature_flags", {}) or {}
        _FLAGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        _FLAGS_PATH.write_text(json.dumps(flags, indent=2))
        log.info("Synced feature flags from server: %s", flags)
        return flags
    except Exception as e:
        log.debug("feature flag sync failed: %s", e)
        return {}


def cached_flags() -> dict:
    """Read the cached flags from disk. Returns {} if no cache."""
    try:
        if _FLAGS_PATH.exists():
            return json.loads(_FLAGS_PATH.read_text())
    except Exception:
        pass
    return {}
