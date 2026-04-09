"""Simple SQLite event store for telemetry and admin dashboard."""

import json
import sqlite3
import os
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = os.environ.get("DEJA_DB_PATH", "/tmp/deja-events.db")

# Ensure parent directory exists
Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)


def _get_db() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            event TEXT NOT NULL,
            user_email TEXT,
            client_version TEXT,
            request_id TEXT,
            properties TEXT,
            component TEXT
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS idx_events_request_id ON events(request_id)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_events_user_email ON events(user_email)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_events_event ON events(event)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events(timestamp)")
    db.commit()
    return db


def store_event(
    event: str,
    properties: dict,
    user_email: str | None,
    client_version: str,
) -> None:
    """Store a telemetry event in the database."""
    db = _get_db()
    db.execute(
        """INSERT INTO events (timestamp, event, user_email, client_version, request_id, properties, component)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            datetime.now(timezone.utc).isoformat(),
            event,
            user_email,
            client_version,
            properties.get("request_id", ""),
            json.dumps(properties),
            properties.get("component", ""),
        ),
    )
    db.commit()
    db.close()


def search_events(
    query: str = "",
    event_type: str = "",
    limit: int = 100,
) -> list[dict]:
    """Search events by request ID, email, or event type."""
    db = _get_db()

    conditions = []
    params = []

    if query:
        conditions.append("(request_id LIKE ? OR user_email LIKE ? OR properties LIKE ?)")
        q = f"%{query}%"
        params.extend([q, q, q])

    if event_type:
        conditions.append("event = ?")
        params.append(event_type)

    where = "WHERE " + " AND ".join(conditions) if conditions else ""

    rows = db.execute(
        f"SELECT * FROM events {where} ORDER BY timestamp DESC LIMIT ?",
        params + [limit],
    ).fetchall()
    db.close()

    return [dict(r) for r in rows]


def get_stats() -> dict:
    """Get summary stats for the dashboard."""
    db = _get_db()

    total = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    errors = db.execute("SELECT COUNT(*) FROM events WHERE event = 'error'").fetchone()[0]
    users = db.execute("SELECT COUNT(DISTINCT user_email) FROM events WHERE user_email IS NOT NULL").fetchone()[0]

    recent_errors = db.execute(
        "SELECT * FROM events WHERE event = 'error' ORDER BY timestamp DESC LIMIT 10"
    ).fetchall()

    active_users = db.execute(
        """SELECT user_email, COUNT(*) as event_count, MAX(timestamp) as last_seen
           FROM events WHERE user_email IS NOT NULL
           GROUP BY user_email ORDER BY last_seen DESC LIMIT 20"""
    ).fetchall()

    db.close()

    return {
        "total_events": total,
        "total_errors": errors,
        "unique_users": users,
        "recent_errors": [dict(r) for r in recent_errors],
        "active_users": [dict(r) for r in active_users],
    }
