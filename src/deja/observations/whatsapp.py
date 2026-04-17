"""Collect recent WhatsApp messages from ChatStorage.sqlite.

Two entry points:

  * ``WhatsAppObserver.collect`` (aliased as ``collect_whatsapp``) —
    steady-state live collector; one ``Observation`` per speaker-turn,
    reading the per-turn buffer written by the Swift app.
  * ``fetch_whatsapp_contacts_backfill`` — onboarding path; one
    ``Observation`` per historical message in every qualifying chat,
    each tagged with a stable ``chat_id`` so the thread reconstructs
    at format time.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from deja.config import DEJA_HOME, WHATSAPP_DB
from deja.observations.base import BaseObserver
from deja.observations.types import Observation

log = logging.getLogger(__name__)


# Apple Core Data epoch — WhatsApp stores ZMESSAGEDATE as seconds since
# 2001-01-01 UTC, same as iMessage but in seconds rather than nanoseconds.
_APPLE_EPOCH_OFFSET = 978307200

# Buffer file written by the Swift app
_WHATSAPP_BUFFER = DEJA_HOME / "whatsapp_buffer.json"


class WhatsAppObserver(BaseObserver):
    """Collects recent WhatsApp messages from the JSON buffer written by the Swift app."""

    def __init__(self, since_minutes: int = 5, limit: int = 20) -> None:
        self.since_minutes = since_minutes
        self.limit = limit

    @property
    def name(self) -> str:
        return "WhatsApp"

    def collect(self) -> list[Observation]:
        return _collect_whatsapp(since_minutes=self.since_minutes, limit=self.limit)


def collect_whatsapp(since_minutes: int = 5, limit: int = 20) -> list[Observation]:
    """Legacy function entry point — delegates to _collect_whatsapp."""
    return _collect_whatsapp(since_minutes=since_minutes, limit=limit)


def _collect_whatsapp(since_minutes: int = 5, limit: int = 20) -> list[Observation]:
    """Read recent WhatsApp messages from the JSON buffer written by the Swift app.

    The Swift app reads ChatStorage.sqlite every 15 seconds and writes
    the results to ~/.deja/whatsapp_buffer.json. This function reads
    that buffer instead of accessing the database directly, so the
    Python process does not need Full Disk Access.

    New per-turn contract: one Observation per speaker-turn, with
    ``chat_id`` (stable ZWACHATSESSION.Z_PK), ``chat_label``, and
    ``speaker``. Legacy buffer rows without those fields fall back to
    synthesized values from ``sender``.
    """
    from deja.observations.contacts import resolve_contact, name_with_handle

    results: list[Observation] = []
    try:
        if not _WHATSAPP_BUFFER.exists():
            return results
        data = json.loads(_WHATSAPP_BUFFER.read_text())
        for r in data[:limit]:
            text = (r.get("text") or "")[:500]
            dt = r.get("dt", "")
            if not text:
                continue

            chat_id = r.get("chat_id") or None
            chat_label = r.get("chat_label") or None
            raw_speaker = r.get("speaker")

            if raw_speaker is None:
                legacy_sender = r.get("sender", "unknown")
                raw_speaker = legacy_sender
                if chat_id is None:
                    chat_id = f"wa-legacy-{legacy_sender}"
                if chat_label is None:
                    chat_label = legacy_sender

            if raw_speaker == "me":
                speaker = "You"
            else:
                resolved = resolve_contact(raw_speaker) or raw_speaker
                if raw_speaker and ("+" in raw_speaker or raw_speaker.isdigit()):
                    speaker = name_with_handle(resolved, raw_speaker)
                else:
                    speaker = resolved

            sender = chat_label or speaker

            speaker_hash = hashlib.md5((raw_speaker or "").encode()).hexdigest()[:6]
            text_hash = hashlib.md5(text.encode()).hexdigest()[:12]
            id_key = f"wa-{chat_id}-{speaker_hash}-{dt}-{text_hash}"

            try:
                ts = datetime.strptime(dt, "%Y-%m-%d %H:%M:%S")
            except (ValueError, TypeError):
                ts = datetime.now()

            results.append(
                Observation(
                    source="whatsapp",
                    sender=sender,
                    text=text,
                    timestamp=ts,
                    id_key=id_key,
                    chat_id=chat_id,
                    chat_label=chat_label,
                    speaker=speaker,
                )
            )
    except Exception:
        log.exception("WhatsApp collection failed")
    return results


# ---------------------------------------------------------------------------
# Onboarding backfill
# ---------------------------------------------------------------------------


def fetch_whatsapp_contacts_backfill(
    days: int = 30,
    messages_per_contact: int = 100,
    min_outbound_messages: int = 3,
    max_chats: int = 200,
) -> list[Observation]:
    """Return one ``Observation`` per active WhatsApp chat in the window.

    Mirror of ``fetch_imessage_contacts_backfill`` against the WhatsApp
    SQLite schema (``ZWAMESSAGE``, ``ZWACHATSESSION``). A chat
    qualifies if the user sent at least ``min_outbound_messages``
    messages during the window. Both 1:1 and group chats are included;
    in WhatsApp a group's ``ZCONTACTJID`` ends with ``@g.us`` and a
    1:1's ends with ``@s.whatsapp.net``.

    Full Disk Access is required. Caller should pre-check the DB.
    """
    if not WHATSAPP_DB.exists():
        log.warning("WhatsApp DB not at %s — skipping backfill", WHATSAPP_DB)
        return []

    cutoff_unix = (datetime.now() - timedelta(days=days)).timestamp()
    cutoff_apple = cutoff_unix - _APPLE_EPOCH_OFFSET

    results: list[Observation] = []
    try:
        conn = sqlite3.connect(f"file:{WHATSAPP_DB}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row

        # Step 1: Qualifying chat sessions.
        session_rows = conn.execute(
            """
            SELECT
                s.Z_PK AS session_id,
                s.ZCONTACTJID AS jid,
                s.ZPARTNERNAME AS partner_name,
                COUNT(CASE WHEN m.ZISFROMME = 1 THEN 1 END) AS outbound_count,
                COUNT(*) AS total_count,
                MAX(m.ZMESSAGEDATE) AS last_date
            FROM ZWACHATSESSION s
            JOIN ZWAMESSAGE m ON m.ZCHATSESSION = s.Z_PK
            WHERE m.ZMESSAGEDATE > ?
            GROUP BY s.Z_PK
            HAVING outbound_count >= ?
            ORDER BY last_date DESC
            LIMIT ?
            """,
            (cutoff_apple, min_outbound_messages, max_chats),
        ).fetchall()

        log.info(
            "WhatsApp backfill: %d qualifying chats "
            "(>=%d outbound in last %d days)",
            len(session_rows), min_outbound_messages, days,
        )

        from deja.observations.contacts import resolve_contact, name_with_handle

        for session in session_rows:
            session_id = session["session_id"]
            stable_chat_id = f"wa-chat-{session_id}"
            jid = (session["jid"] or "").strip()
            partner_name = (session["partner_name"] or "").strip()
            is_group = jid.endswith("@g.us")

            if is_group:
                chat_label = partner_name or jid
                one_to_one_handle: str | None = None
            else:
                raw_phone = jid.split("@", 1)[0] if "@" in jid else jid
                resolved = resolve_contact(raw_phone) or partner_name or raw_phone
                chat_label = name_with_handle(resolved, raw_phone)
                one_to_one_handle = raw_phone

            # Step 2: Last N messages in this session.
            msg_rows = conn.execute(
                """
                SELECT
                    m.ZISFROMME AS is_from_me,
                    m.ZTEXT AS text,
                    m.ZMESSAGEDATE AS msg_date,
                    m.ZFROMJID AS from_jid,
                    datetime(m.ZMESSAGEDATE + 978307200, 'unixepoch', 'localtime') AS dt
                FROM ZWAMESSAGE m
                WHERE m.ZCHATSESSION = ?
                  AND m.ZMESSAGEDATE > ?
                  AND m.ZTEXT IS NOT NULL
                  AND m.ZTEXT != ''
                ORDER BY m.ZMESSAGEDATE DESC
                LIMIT ?
                """,
                (session_id, cutoff_apple, messages_per_contact),
            ).fetchall()

            msg_rows = list(reversed(msg_rows))
            if not msg_rows:
                continue

            # Emit one Observation per message. Shared chat_id keeps the
            # thread reconstructable at format time; per-turn speaker
            # prevents cross-speaker attribution drift in groups.
            for m in msg_rows:
                body = (m["text"] or "").replace("\n", " ").strip()
                if not body:
                    continue
                body = body[:500]

                if m["is_from_me"]:
                    speaker = "You"
                    raw_speaker = "me"
                elif is_group:
                    raw = (m["from_jid"] or "").split("@", 1)[0] if m["from_jid"] else ""
                    if raw:
                        resolved_name = resolve_contact(raw) or raw
                        speaker = name_with_handle(resolved_name, raw)
                    else:
                        speaker = "unknown"
                    raw_speaker = raw or "unknown"
                else:
                    # 1:1 — sender is always the other party.
                    name_only = (
                        resolve_contact(one_to_one_handle)
                        if one_to_one_handle else None
                    ) or partner_name or (one_to_one_handle or "unknown")
                    speaker = name_with_handle(name_only, one_to_one_handle or "")
                    raw_speaker = one_to_one_handle or "unknown"

                try:
                    ts = datetime.strptime(m["dt"], "%Y-%m-%d %H:%M:%S")
                except (ValueError, TypeError, KeyError):
                    ts = datetime.now()

                speaker_hash = hashlib.md5(raw_speaker.encode()).hexdigest()[:6]
                text_hash = hashlib.md5(body.encode()).hexdigest()[:12]
                # ZMESSAGEDATE is a float; format with fixed precision so
                # the id_key stays stable across runs.
                date_key = f"{m['msg_date']:.3f}" if m["msg_date"] is not None else "0"
                id_key = (
                    f"wa-backfill-{session_id}-{speaker_hash}-"
                    f"{date_key}-{text_hash}"
                )

                results.append(Observation(
                    source="whatsapp",
                    sender=chat_label[:100],
                    text=body,
                    timestamp=ts,
                    id_key=id_key,
                    chat_id=stable_chat_id,
                    chat_label=chat_label,
                    speaker=speaker,
                ))

        conn.close()
    except sqlite3.Error as e:
        log.warning(
            "WhatsApp backfill failed (likely Full Disk Access not granted): %s",
            e,
        )
        return []
    except Exception:
        log.exception("WhatsApp backfill failed with unexpected error")
        return []

    results.sort(key=lambda o: o.timestamp)
    log.info("WhatsApp backfill: returning %d per-turn observations", len(results))
    return results
