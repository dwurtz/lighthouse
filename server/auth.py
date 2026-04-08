"""Google ID token validation with short-lived cache."""

import time

from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
from fastapi import HTTPException

# Cache: token -> (user_info, expiry_time)
_cache: dict[str, tuple[dict, float]] = {}
_CACHE_TTL = 300  # 5 minutes

_http_request = google_requests.Request()


async def validate_token(token: str) -> dict:
    """Validate a Google ID token and return {"email": str, "name": str}.

    Raises HTTPException(401) on invalid tokens.
    """
    # Check cache
    now = time.time()
    if token in _cache:
        user_info, expiry = _cache[token]
        if now < expiry:
            return user_info

    try:
        # Pass clock_skew_in_seconds to be lenient with slight time drift.
        # Not specifying audience accepts tokens from any Google client ID.
        id_info = id_token.verify_oauth2_token(
            token, _http_request, audience=None, clock_skew_in_seconds=10
        )
    except Exception as exc:
        raise HTTPException(status_code=401, detail=f"Invalid token: {exc}")

    user_info = {
        "email": id_info.get("email", "unknown"),
        "name": id_info.get("name", ""),
    }

    _cache[token] = (user_info, now + _CACHE_TTL)

    # Evict stale entries periodically
    if len(_cache) > 200:
        _cache.clear()

    return user_info
