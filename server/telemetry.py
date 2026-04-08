"""Structured event logging to stdout (captured by Render)."""

import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def log_event(
    event: str,
    properties: dict,
    user_email: str | None,
    client_version: str,
) -> None:
    """Log a client telemetry event as structured JSON to stdout."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "properties": properties,
        "user_email": user_email,
        "client_version": client_version,
    }
    logger.info(json.dumps(entry))
