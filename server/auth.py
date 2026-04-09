"""Google ID/access token validation with short-lived cache."""

import hashlib
import logging
import os
import time

from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
from fastapi import HTTPException

logger = logging.getLogger(__name__)

# Cache: token_hash -> (user_info, expiry_time)
_cache: dict[str, tuple[dict, float]] = {}
_CACHE_TTL = 300  # 5 minutes

_http_request = google_requests.Request()

# Optional: restrict to specific users or domains
_ALLOWED_EMAILS = set(filter(None, os.environ.get("DEJA_ALLOWED_EMAILS", "").split(",")))
_ALLOWED_DOMAINS = set(filter(None, os.environ.get("DEJA_ALLOWED_DOMAINS", "").split(",")))


async def validate_token(token: str) -> dict:
    """Validate a Google token and return {"email": str, "name": str}.

    Raises HTTPException(401) on invalid tokens.
    Raises HTTPException(403) if user is not authorized.
    """
    # Use token hash as cache key (don't store raw tokens in memory)
    cache_key = hashlib.sha256(token.encode()).hexdigest()[:16]

    now = time.time()
    if cache_key in _cache:
        user_info, expiry = _cache[cache_key]
        if now < expiry:
            return user_info

    try:
        id_info = id_token.verify_oauth2_token(
            token, _http_request, audience=None, clock_skew_in_seconds=10
        )
    except Exception as exc:
        logger.warning("Token validation failed: %s", type(exc).__name__)
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    email = id_info.get("email", "")
    domain = email.split("@")[1] if "@" in email else ""

    # Authorization check — if allowlists are configured, enforce them
    if _ALLOWED_EMAILS or _ALLOWED_DOMAINS:
        if email not in _ALLOWED_EMAILS and domain not in _ALLOWED_DOMAINS:
            logger.warning("Unauthorized user: %s", email)
            raise HTTPException(status_code=403, detail="User not authorized")

    user_info = {
        "email": email or "unknown",
        "name": id_info.get("name", ""),
    }

    _cache[cache_key] = (user_info, now + _CACHE_TTL)

    # Evict stale entries periodically
    if len(_cache) > 200:
        _cache.clear()

    return user_info
