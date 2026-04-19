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
    db.execute("""
        CREATE TABLE IF NOT EXISTS diagnostics (
            id TEXT PRIMARY KEY,
            timestamp TEXT NOT NULL,
            user_email TEXT,
            client_version TEXT,
            note TEXT,
            bundle TEXT NOT NULL,
            size_bytes INTEGER
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS idx_diagnostics_user_email ON diagnostics(user_email)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_diagnostics_timestamp ON diagnostics(timestamp)")

    # Mobile signal channel — notes sent from iOS Shortcuts (Action Button,
    # Back Tap, etc.) land here, get drained by local Deja on its next poll,
    # and feed into the chief-of-staff command pipeline.
    db.execute("""
        CREATE TABLE IF NOT EXISTS mobile_inbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_email TEXT NOT NULL,
            created_at TEXT NOT NULL,
            source TEXT NOT NULL,
            kind TEXT NOT NULL,
            text TEXT,
            delivered_at TEXT
        )
    """)
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_inbox_pending ON mobile_inbox(user_email, delivered_at)"
    )

    # Long-lived mobile API keys. iOS Shortcuts store these as a
    # password field; paired with user_email on create so we can route
    # inbound posts to the right user without re-authenticating Google
    # on every call.
    db.execute("""
        CREATE TABLE IF NOT EXISTS mobile_keys (
            key_hash TEXT PRIMARY KEY,
            user_email TEXT NOT NULL,
            label TEXT NOT NULL,
            created_at TEXT NOT NULL,
            last_used_at TEXT
        )
    """)
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_mkeys_user ON mobile_keys(user_email)"
    )

    db.commit()
    return db


# ---------------------------------------------------------------------------
# Mobile inbox helpers
# ---------------------------------------------------------------------------


def _hash_key(plaintext: str) -> str:
    import hashlib
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def mobile_key_create(user_email: str, label: str) -> str:
    """Generate a fresh mobile API key, store its hash, return the plaintext.

    Plaintext is returned ONCE to the caller — only the hash persists.
    """
    import secrets
    plaintext = "deja_" + secrets.token_urlsafe(32)
    db = _get_db()
    db.execute(
        """INSERT INTO mobile_keys (key_hash, user_email, label, created_at)
           VALUES (?, ?, ?, ?)""",
        (
            _hash_key(plaintext),
            user_email,
            label,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    db.commit()
    return plaintext


def mobile_key_lookup(plaintext: str) -> str | None:
    """Return user_email for a valid key, else None. Touches last_used_at."""
    if not plaintext or not plaintext.startswith("deja_"):
        return None
    key_hash = _hash_key(plaintext)
    db = _get_db()
    row = db.execute(
        "SELECT user_email FROM mobile_keys WHERE key_hash = ?",
        (key_hash,),
    ).fetchone()
    if not row:
        return None
    db.execute(
        "UPDATE mobile_keys SET last_used_at = ? WHERE key_hash = ?",
        (datetime.now(timezone.utc).isoformat(), key_hash),
    )
    db.commit()
    return row["user_email"]


def mobile_inbox_put(user_email: str, source: str, kind: str, text: str) -> int:
    db = _get_db()
    cur = db.execute(
        """INSERT INTO mobile_inbox (user_email, created_at, source, kind, text)
           VALUES (?, ?, ?, ?, ?)""",
        (
            user_email,
            datetime.now(timezone.utc).isoformat(),
            source,
            kind,
            text,
        ),
    )
    db.commit()
    return cur.lastrowid or 0


def mobile_inbox_drain(user_email: str) -> list[dict]:
    """Return pending items for user_email, mark them delivered."""
    db = _get_db()
    rows = db.execute(
        """SELECT id, created_at, source, kind, text
           FROM mobile_inbox
           WHERE user_email = ? AND delivered_at IS NULL
           ORDER BY id ASC""",
        (user_email,),
    ).fetchall()
    items = [dict(r) for r in rows]
    if items:
        ids = [r["id"] for r in items]
        placeholders = ",".join("?" * len(ids))
        db.execute(
            f"UPDATE mobile_inbox SET delivered_at = ? WHERE id IN ({placeholders})",
            [datetime.now(timezone.utc).isoformat(), *ids],
        )
        db.commit()
    return items


def store_diagnostic(
    diag_id: str,
    user_email: str | None,
    client_version: str,
    note: str,
    bundle: str,
) -> None:
    db = _get_db()
    db.execute(
        """INSERT INTO diagnostics (id, timestamp, user_email, client_version, note, bundle, size_bytes)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            diag_id,
            datetime.now(timezone.utc).isoformat(),
            user_email,
            client_version,
            note,
            bundle,
            len(bundle.encode("utf-8")),
        ),
    )
    db.commit()
    db.close()


def list_diagnostics(limit: int = 100, email: str | None = None) -> list[dict]:
    db = _get_db()
    if email:
        rows = db.execute(
            """SELECT id, timestamp, user_email, client_version, note, size_bytes
               FROM diagnostics WHERE user_email = ?
               ORDER BY timestamp DESC LIMIT ?""",
            (email, limit),
        ).fetchall()
    else:
        rows = db.execute(
            """SELECT id, timestamp, user_email, client_version, note, size_bytes
               FROM diagnostics ORDER BY timestamp DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    db.close()
    return [dict(r) for r in rows]


def get_diagnostic(diag_id: str) -> dict | None:
    db = _get_db()
    row = db.execute(
        "SELECT * FROM diagnostics WHERE id = ?",
        (diag_id,),
    ).fetchone()
    db.close()
    return dict(row) if row else None


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


def get_user_detail(email: str, limit: int = 100) -> dict:
    """Per-user summary + recent event timeline for the admin drill-down.

    Returns aggregate counts, the event-type breakdown, and a timeline
    of the N most recent events. Drives the admin dashboard's user
    detail page.
    """
    db = _get_db()

    profile = db.execute(
        """SELECT
             MIN(timestamp) as first_seen,
             MAX(timestamp) as last_seen,
             COUNT(*) as total_events,
             COUNT(DISTINCT client_version) as version_count,
             SUM(CASE WHEN event = 'error' THEN 1 ELSE 0 END) as errors,
             SUM(CASE WHEN event = 'llm_call' THEN 1 ELSE 0 END) as llm_calls,
             SUM(CASE WHEN event = 'cycle_completed' THEN 1 ELSE 0 END) as cycles,
             SUM(CASE WHEN event = 'command_dispatched' THEN 1 ELSE 0 END) as commands
           FROM events WHERE user_email = ?""",
        (email,),
    ).fetchone()

    # Most recent client_version this user reported — useful to know
    # which build they're on when debugging support issues.
    version_row = db.execute(
        """SELECT client_version FROM events
           WHERE user_email = ? AND client_version IS NOT NULL AND client_version != ''
           ORDER BY timestamp DESC LIMIT 1""",
        (email,),
    ).fetchone()

    event_breakdown = db.execute(
        """SELECT event, COUNT(*) as count FROM events
           WHERE user_email = ?
           GROUP BY event ORDER BY count DESC LIMIT 20""",
        (email,),
    ).fetchall()

    recent = db.execute(
        """SELECT * FROM events WHERE user_email = ?
           ORDER BY timestamp DESC LIMIT ?""",
        (email, limit),
    ).fetchall()

    db.close()

    return {
        "email": email,
        "profile": dict(profile) if profile else {},
        "current_version": version_row["client_version"] if version_row else "unknown",
        "event_breakdown": [dict(r) for r in event_breakdown],
        "recent_events": [dict(r) for r in recent],
    }


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
