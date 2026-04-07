"""Collect recent WhatsApp messages from ChatStorage.sqlite.

Two entry points:

  * ``collect_whatsapp`` — steady-state live collector; one
    ``Observation`` per recent message.
  * ``fetch_whatsapp_contacts_backfill`` — onboarding path; one
    ``Observation`` per active chat (1:1 or group) over the last N
    days, with a digest of the last M messages in each.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from datetime import datetime, timedelta

from deja.config import WHATSAPP_DB
from deja.observations.types import Observation

log = logging.getLogger(__name__)


# Apple Core Data epoch — WhatsApp stores ZMESSAGEDATE as seconds since
# 2001-01-01 UTC, same as iMessage but in seconds rather than nanoseconds.
_APPLE_EPOCH_OFFSET = 978307200


def collect_whatsapp(since_minutes: int = 5, limit: int = 20) -> list[Observation]:
    """Read recent WhatsApp messages from ChatStorage.sqlite."""
    results: list[Observation] = []
    try:
        if not WHATSAPP_DB.exists():
            return results
        conn = sqlite3.connect(str(WHATSAPP_DB))
        conn.row_factory = sqlite3.Row
        cutoff_unix = (datetime.now() - timedelta(minutes=since_minutes)).timestamp()
        # WhatsApp stores ZMESSAGEDATE as seconds since Apple epoch (2001-01-01)
        cutoff_apple = cutoff_unix - 978307200
        rows = conn.execute(
            """
            SELECT
                m.Z_PK,
                m.ZTEXT as text,
                datetime(m.ZMESSAGEDATE + 978307200, 'unixepoch', 'localtime') as dt,
                CASE WHEN m.ZISFROMME = 1 THEN 'me' ELSE
                    COALESCE(s.ZCONTACTJID, 'unknown')
                END as sender
            FROM ZWAMESSAGE m
            LEFT JOIN ZWACHATSESSION s ON m.ZCHATSESSION = s.Z_PK
            WHERE m.ZTEXT IS NOT NULL AND m.ZTEXT != '' AND m.ZMESSAGEDATE > ?
            ORDER BY m.ZMESSAGEDATE DESC
            LIMIT ?
            """,
            (cutoff_apple, limit),
        ).fetchall()
        conn.close()
        for r in rows:
            sender = "You" if r["sender"] == "me" else r["sender"]
            text = (r["text"] or "")[:500]
            text_hash = hashlib.md5(text.encode()).hexdigest()[:12]
            id_key = f"wa-{sender}-{r['dt']}-{text_hash}"
            try:
                ts = datetime.strptime(r["dt"], "%Y-%m-%d %H:%M:%S")
            except (ValueError, TypeError):
                ts = datetime.now()
            results.append(
                Observation(
                    source="whatsapp",
                    sender=sender,
                    text=text,
                    timestamp=ts,
                    id_key=id_key,
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

        from deja.observations.contacts import resolve_contact

        def _name_with_handle(name: str, handle: str) -> str:
            """Render 'Jane Doe (+15551234567)' or just the handle if
            contact resolution failed. Matches the iMessage convention."""
            if not handle or name == handle:
                return name
            return f"{name} ({handle})"

        for session in session_rows:
            session_id = session["session_id"]
            jid = (session["jid"] or "").strip()
            partner_name = (session["partner_name"] or "").strip()
            is_group = jid.endswith("@g.us")

            if is_group:
                # Group chats — session gives us only the group name.
                # Per-sender handles come in via ZFROMJID on each message.
                group_title = partner_name or jid
                chat_label = f"WhatsApp group '{group_title}'"
                sender_label = chat_label
                # 1:1 handle not applicable for groups
                one_to_one_handle: str | None = None
            else:
                # 1:1 — jid contains the raw phone (or email for business
                # accounts). Strip the @s.whatsapp.net suffix.
                raw_phone = jid.split("@", 1)[0] if "@" in jid else jid
                resolved = resolve_contact(raw_phone) or partner_name or raw_phone
                chat_label = _name_with_handle(resolved, raw_phone)
                sender_label = chat_label
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

            lines: list[str] = []
            lines.append(
                f"WhatsApp with {chat_label} — "
                f"{session['total_count']} msgs in last {days} days "
                f"({session['outbound_count']} from user)"
            )

            # For groups, pre-walk messages to collect the unique set of
            # participant handles that appear in the window, then emit a
            # Participants: line so the LLM sees every phone number that
            # belongs to this group in one place (same as iMessage).
            if is_group:
                seen_handles: dict[str, str] = {}
                for m in msg_rows:
                    if m["is_from_me"] or not m["from_jid"]:
                        continue
                    raw = m["from_jid"].split("@", 1)[0]
                    if raw and raw not in seen_handles:
                        seen_handles[raw] = resolve_contact(raw) or raw
                if seen_handles:
                    participants_line = "; ".join(
                        _name_with_handle(name, raw)
                        for raw, name in seen_handles.items()
                    )
                    lines.append(f"Participants: {participants_line}")

            for m in msg_rows:
                if m["is_from_me"]:
                    who = "You"
                elif is_group:
                    # In group chats ZFROMJID identifies the specific sender.
                    raw = (m["from_jid"] or "").split("@", 1)[0] if m["from_jid"] else ""
                    if raw:
                        resolved = resolve_contact(raw) or raw
                        who = _name_with_handle(resolved, raw)
                    else:
                        who = "unknown"
                else:
                    # 1:1 — we already know the sender; show with handle.
                    name_only = (
                        resolve_contact(one_to_one_handle)
                        if one_to_one_handle else None
                    ) or partner_name or (one_to_one_handle or "unknown")
                    who = _name_with_handle(name_only, one_to_one_handle or "")
                text = (m["text"] or "").replace("\n", " ").strip()[:300]
                lines.append(f"  [{m['dt']}] {who}: {text}")

            digest = "\n".join(lines)[:4000]

            try:
                last_dt_str = msg_rows[-1]["dt"]
                ts = datetime.strptime(last_dt_str, "%Y-%m-%d %H:%M:%S")
            except (ValueError, TypeError, KeyError):
                ts = datetime.now()

            id_key = f"wa-backfill-{session_id}-{session['last_date']}"

            results.append(Observation(
                source="whatsapp",
                sender=sender_label[:100],
                text=digest,
                timestamp=ts,
                id_key=id_key,
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
    log.info("WhatsApp backfill: returning %d conversation digests", len(results))
    return results
