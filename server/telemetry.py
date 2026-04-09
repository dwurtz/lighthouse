"""Structured event logging to stdout + SQLite database."""

import json
import logging
from datetime import datetime, timezone

from db import store_event

logger = logging.getLogger(__name__)


def log_event(
    event: str,
    properties: dict,
    user_email: str | None,
    client_version: str,
) -> None:
    """Log a client telemetry event to stdout and persist in database."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "properties": properties,
        "user_email": user_email,
        "client_version": client_version,
    }
    logger.info(json.dumps(entry))

    # Persist for dashboard queries
    try:
        store_event(event, properties, user_email, client_version)
    except Exception:
        logger.exception("Failed to store event in database")
