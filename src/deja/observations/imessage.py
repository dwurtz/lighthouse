"""Collect recent iMessages from chat.db.

Two entry points:

  * ``collect_imessages`` — the steady-state live collector used by
    the monitor's 3-second observation cycle. Returns one
    ``Observation`` per *recent message*.
  * ``fetch_imessage_contacts_backfill`` — the onboarding path used
    once per user. Returns one ``Observation`` per *conversation* over
    the last N days, with each observation's text being a compact
    digest of the last M messages in that chat. Used by the onboarding
    runner to bootstrap the wiki with people/project pages built from
    historical chat context.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from deja.config import DEJA_HOME, IMESSAGE_DB
from deja.observations.types import Observation

log = logging.getLogger(__name__)


# Apple's Core Data epoch is 2001-01-01 UTC. chat.db stores `message.date`
# as nanoseconds since that epoch (post-macOS 10.13) or seconds (earlier).
_APPLE_EPOCH_OFFSET = 978307200

# Buffer file written by the Swift app
_IMESSAGE_BUFFER = DEJA_HOME / "imessage_buffer.json"


def collect_imessages(since_minutes: int = 5, limit: int = 20) -> list[Observation]:
    """Read recent iMessages from the JSON buffer written by the Swift app.

    The Swift app reads ~/Library/Messages/chat.db every 15 seconds and
    writes the results to ~/.deja/imessage_buffer.json. This function
    reads that buffer instead of accessing the database directly, so
    the Python process does not need Full Disk Access.
    """
    results: list[Observation] = []
    try:
        if not _IMESSAGE_BUFFER.exists():
            return results
        data = json.loads(_IMESSAGE_BUFFER.read_text())
        for r in data[:limit]:
            sender = "You" if r.get("sender") == "me" else r.get("sender", "unknown")
            text = (r.get("text") or "")[:500]
            dt = r.get("dt", "")
            text_hash = hashlib.md5(text.encode()).hexdigest()[:12]
            id_key = f"imsg-{sender}-{dt}-{text_hash}"
            try:
                ts = datetime.strptime(dt, "%Y-%m-%d %H:%M:%S")
            except (ValueError, TypeError):
                ts = datetime.now()
            results.append(
                Observation(
                    source="imessage",
                    sender=sender,
                    text=text,
                    timestamp=ts,
                    id_key=id_key,
                )
            )
    except Exception:
        log.exception("iMessage collection failed")
    return results


# ---------------------------------------------------------------------------
# Onboarding backfill
# ---------------------------------------------------------------------------


def fetch_imessage_contacts_backfill(
    days: int = 30,
    messages_per_contact: int = 100,
    min_outbound_messages: int = 3,
    max_chats: int = 200,
) -> list[Observation]:
    """Return one ``Observation`` per active chat in the last ``days`` days.

    "Active" means: the user sent at least ``min_outbound_messages``
    messages in that chat during the window. Filters out one-off or
    inbound-only exchanges (2FA codes, spam, wrong-number texts the
    user never answered).

    For each qualifying chat — 1:1 or group — the function pulls the
    last ``messages_per_contact`` messages from that chat (oldest
    first) and packs them into a single digest text. The text is
    capped so no individual observation blows past the LLM batch
    budget.

    This is onboarding-only. The steady-state monitor loop uses
    ``collect_imessages`` instead, which returns one observation per
    message.

    Full Disk Access is required. Caller (the onboarding runner) is
    expected to probe the DB with a pre-check before calling this.
    """
    if not IMESSAGE_DB.exists():
        log.warning("iMessage DB not at %s — skipping backfill", IMESSAGE_DB)
        return []

    cutoff_unix = (datetime.now() - timedelta(days=days)).timestamp()
    cutoff_apple_ns = int((cutoff_unix - _APPLE_EPOCH_OFFSET) * 1_000_000_000)

    results: list[Observation] = []
    try:
        conn = sqlite3.connect(f"file:{IMESSAGE_DB}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row

        # Step 1: Find every chat the user sent >= N messages in during
        # the window, ordered by most recent activity. chat_message_join
        # links messages to chats (needed for group-chat grouping —
        # message.handle_id alone only identifies the *sender*, not the
        # room). display_name is populated for group chats; for 1:1s
        # it's empty and we fall back to the other participant's handle.
        chat_rows = conn.execute(
            """
            SELECT
                c.ROWID AS chat_id,
                c.chat_identifier,
                c.display_name,
                c.style,
                COUNT(CASE WHEN m.is_from_me = 1 THEN 1 END) AS outbound_count,
                COUNT(*) AS total_count,
                MAX(m.date) AS last_date
            FROM chat c
            JOIN chat_message_join cmj ON cmj.chat_id = c.ROWID
            JOIN message m ON m.ROWID = cmj.message_id
            WHERE m.date > ?
            GROUP BY c.ROWID
            HAVING outbound_count >= ?
            ORDER BY last_date DESC
            LIMIT ?
            """,
            (cutoff_apple_ns, min_outbound_messages, max_chats),
        ).fetchall()

        log.info(
            "iMessage backfill: %d qualifying chats "
            "(>=%d outbound in last %d days)",
            len(chat_rows), min_outbound_messages, days,
        )

        # Step 2: Pre-resolve the participant handles for each chat so
        # we can render group chats as "Alice, Bob, Carol" and 1:1s as
        # "Alice" in the sender field. We need chat_handle_join for
        # this (messages only carry the sender handle, not the full
        # participant list of the chat).
        from deja.observations.contacts import resolve_contact

        for chat in chat_rows:
            chat_id = chat["chat_id"]
            is_group = (chat["style"] == 43)  # style 43 = group, 45 = 1:1
            display_name = (chat["display_name"] or "").strip()

            # Who's in this chat (excluding the user themselves — the
            # user is implicit).
            handle_rows = conn.execute(
                """
                SELECT h.id
                FROM chat_handle_join chj
                JOIN handle h ON h.ROWID = chj.handle_id
                WHERE chj.chat_id = ?
                """,
                (chat_id,),
            ).fetchall()
            participant_handles = [h["id"] for h in handle_rows if h["id"]]

            # Resolve each handle to a contact name when possible, and
            # keep a (name, raw_handle) pair so we can surface the raw
            # identifier in the digest — the LLM needs it to capture
            # phone numbers and emails into people-page frontmatter.
            participant_pairs: list[tuple[str, str]] = []
            for handle in participant_handles:
                resolved = resolve_contact(handle) or handle
                participant_pairs.append((resolved, handle))
            participant_names = [name for name, _ in participant_pairs]

            def _name_with_handle(name: str, handle: str) -> str:
                """Render 'Jane Doe (+15551234567)', or just the handle
                if name and handle are already the same string (i.e.
                contact resolution failed and name IS the handle)."""
                if name == handle or not handle:
                    return name
                return f"{name} ({handle})"

            # Concise sender_label for the Observation row (100-char cap
            # enforced downstream). The FULL participant list with handles
            # goes in the digest text, where the 4KB cap gives plenty of
            # room — that's where the LLM actually reads the identifiers.
            if is_group:
                if display_name:
                    chat_label_short = f"group '{display_name}' ({len(participant_pairs)} members)"
                else:
                    short_names = ", ".join(participant_names[:3])
                    extra = f" +{len(participant_names) - 3}" if len(participant_names) > 3 else ""
                    chat_label_short = f"group ({short_names}{extra})"
                sender_label = f"iMessage {chat_label_short}"
            else:
                if participant_pairs:
                    name, handle = participant_pairs[0]
                    chat_label_short = _name_with_handle(name, handle)
                else:
                    chat_label_short = "unknown"
                sender_label = chat_label_short

            # Step 3: Pull the last N messages in this chat. Join
            # handle so we can attribute each inbound message to a
            # specific sender (important in group chats).
            msg_rows = conn.execute(
                """
                SELECT
                    m.is_from_me,
                    m.text,
                    m.date,
                    h.id AS sender_handle,
                    datetime(m.date/1000000000 + 978307200, 'unixepoch', 'localtime') AS dt
                FROM chat_message_join cmj
                JOIN message m ON m.ROWID = cmj.message_id
                LEFT JOIN handle h ON m.handle_id = h.ROWID
                WHERE cmj.chat_id = ?
                  AND m.date > ?
                  AND m.text IS NOT NULL
                  AND m.text != ''
                ORDER BY m.date DESC
                LIMIT ?
                """,
                (chat_id, cutoff_apple_ns, messages_per_contact),
            ).fetchall()

            # Reverse so oldest-first (the DB returned newest-first to
            # apply LIMIT correctly).
            msg_rows = list(reversed(msg_rows))

            if not msg_rows:
                continue

            # Build the digest text. The first line summarizes the chat;
            # the second line explicitly lists every participant with
            # their raw handle (phone or email) so the LLM can capture
            # identifiers into people-page frontmatter. Individual
            # messages follow, with each inbound sender shown as
            # "Name (handle)" so the LLM can attribute per-message
            # content in group chats correctly.
            lines: list[str] = []
            if is_group:
                title_label = f"group '{display_name}'" if display_name else "group"
                lines.append(
                    f"iMessage {title_label} — "
                    f"{chat['total_count']} msgs in last {days} days "
                    f"({chat['outbound_count']} from user)"
                )
                participants_line = "; ".join(
                    _name_with_handle(n, h) for n, h in participant_pairs
                )
                lines.append(f"Participants: {participants_line}")
            else:
                # 1:1 — header already contains name + handle
                if participant_pairs:
                    name, handle = participant_pairs[0]
                    header_name = _name_with_handle(name, handle)
                else:
                    header_name = "unknown"
                lines.append(
                    f"iMessage with {header_name} — "
                    f"{chat['total_count']} msgs in last {days} days "
                    f"({chat['outbound_count']} from user)"
                )

            for m in msg_rows:
                if m["is_from_me"]:
                    who = "You"
                else:
                    raw = m["sender_handle"] or "unknown"
                    resolved = resolve_contact(raw) or raw
                    who = _name_with_handle(resolved, raw)
                text = (m["text"] or "").replace("\n", " ").strip()
                # Per-message cap so a long rant can't eat the whole budget.
                text = text[:300]
                lines.append(f"  [{m['dt']}] {who}: {text}")

            # Hard cap the whole digest at ~4KB so batching stays predictable.
            digest = "\n".join(lines)[:4000]

            # Use the most recent message's timestamp as the Observation
            # timestamp so the LLM sees when the conversation was actually
            # active, not "now".
            try:
                last_dt_str = msg_rows[-1]["dt"]
                ts = datetime.strptime(last_dt_str, "%Y-%m-%d %H:%M:%S")
            except (ValueError, TypeError, KeyError):
                ts = datetime.now()

            id_key = f"imsg-backfill-{chat_id}-{chat['last_date']}"

            results.append(Observation(
                source="imessage",
                sender=sender_label[:100],
                text=digest,
                timestamp=ts,
                id_key=id_key,
            ))

        conn.close()
    except sqlite3.Error as e:
        log.warning(
            "iMessage backfill failed (likely Full Disk Access not granted): %s",
            e,
        )
        return []
    except Exception:
        log.exception("iMessage backfill failed with unexpected error")
        return []

    # Sort oldest → newest so batches show the timeline in order.
    results.sort(key=lambda o: o.timestamp)
    log.info("iMessage backfill: returning %d conversation digests", len(results))
    return results
