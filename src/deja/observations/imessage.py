"""Collect recent iMessages from chat.db.

Two entry points:

  * ``IMessageObserver.collect`` (aliased as ``collect_imessages``) —
    the steady-state live collector used by the monitor's 3-second
    observation cycle. Returns one ``Observation`` per speaker-turn,
    reading the per-turn buffer written by the Swift app.
  * ``fetch_imessage_contacts_backfill`` — the onboarding path used
    once per user. Returns one ``Observation`` per *message* across
    every qualifying chat in the last N days. All turns from the same
    chat share a stable ``chat_id``, so format-time reconstruction
    rebuilds the thread cleanly and per-turn ``speaker`` attribution
    stops the integrator from fusing speakers in group chats.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from deja.config import DEJA_HOME, IMESSAGE_DB
from deja.observations.base import BaseObserver
from deja.observations.types import Observation

log = logging.getLogger(__name__)


# Apple's Core Data epoch is 2001-01-01 UTC. chat.db stores `message.date`
# as nanoseconds since that epoch (post-macOS 10.13) or seconds (earlier).
_APPLE_EPOCH_OFFSET = 978307200

# Buffer file written by the Swift app
_IMESSAGE_BUFFER = DEJA_HOME / "imessage_buffer.json"


class IMessageObserver(BaseObserver):
    """Collects recent iMessages from the JSON buffer written by the Swift app."""

    def __init__(self, since_minutes: int = 5, limit: int = 20) -> None:
        self.since_minutes = since_minutes
        self.limit = limit

    @property
    def name(self) -> str:
        return "iMessage"

    def collect(self) -> list[Observation]:
        return _collect_imessages(since_minutes=self.since_minutes, limit=self.limit)


def collect_imessages(since_minutes: int = 5, limit: int = 20) -> list[Observation]:
    """Legacy function entry point — delegates to _collect_imessages."""
    return _collect_imessages(since_minutes=since_minutes, limit=limit)


# ---------------------------------------------------------------------------
# Self-iMessage routing — parallel to the email reply channel
# ---------------------------------------------------------------------------


_PROCESSED_SELF_IMSG = Path.home() / ".deja" / "chief_of_staff" / "processed_self_imessages"


def _is_self_chat_turn(
    raw_speaker: str | None,
    chat_label: str | None,
) -> bool:
    """True iff this turn is the user texting themselves.

    Apple Messages has a single self-chat per Apple ID; chat_identifier
    is the user's own email or phone. After contact resolution it
    typically renders as the user's name. Either rendered form works as
    a signal: match user's email, phone, or display name against the
    chat_label.
    """
    if raw_speaker != "me":
        return False
    try:
        from deja.identity import load_user
        user = load_user()
        email = (user.email or "").lower()
        name = (user.name or "").lower()
    except Exception:
        return False
    label = (chat_label or "").lower()
    if not label:
        return False
    if email and email in label:
        return True
    if name and name and name in label:
        return True
    return False


def _mark_self_imsg_processed(id_key: str) -> None:
    try:
        _PROCESSED_SELF_IMSG.parent.mkdir(parents=True, exist_ok=True)
        with _PROCESSED_SELF_IMSG.open("a", encoding="utf-8") as f:
            f.write(id_key + "\n")
    except Exception:
        log.debug("self-imessage dedupe write failed", exc_info=True)


def _is_self_imsg_processed(id_key: str) -> bool:
    if not _PROCESSED_SELF_IMSG.exists():
        return False
    try:
        return id_key in _PROCESSED_SELF_IMSG.read_text().splitlines()
    except Exception:
        return False


def _dispatch_self_imessage_to_cos(text: str, id_key: str, ts: datetime) -> None:
    """Route a self-iMessage turn straight to cos in user_reply mode.

    Suppresses the normal observation flow — integrate never sees these.
    One conversation file per day (``imessage-self-YYYYMMDD``) so a
    day's self-notes cluster together in ``~/Deja/conversations/``.
    Deduped on id_key (which already encodes speaker + timestamp +
    text hash, collision-resistant across buffer re-reads).
    """
    if _is_self_imsg_processed(id_key):
        return
    body = (text or "").strip()
    if not body:
        return
    day = ts.strftime("%Y%m%d")
    thread_id = f"imessage-self-{day}"
    subject = f"Self-iMessage — {ts.strftime('%Y-%m-%d')}"
    try:
        from deja import chief_of_staff
        chief_of_staff.log_dialogue_turn(
            role="user",
            subject=subject,
            body=body,
            thread_id=thread_id,
            message_id=id_key,
        )
        chief_of_staff.invoke_user_reply(
            subject=subject,
            user_message=body,
            thread_id=thread_id,
            in_reply_to=id_key,
            message_id=id_key,
        )
    except Exception:
        log.exception("self-imessage → cos dispatch failed (id=%s)", id_key)
        return
    _mark_self_imsg_processed(id_key)
    log.info("self-imessage → cos: %r (id=%s)", body[:80], id_key[:40])


def _collect_imessages(since_minutes: int = 5, limit: int = 20) -> list[Observation]:
    """Read recent iMessages from the JSON buffer written by the Swift app.

    The Swift app reads ~/Library/Messages/chat.db every 15 seconds and
    writes the results to ~/.deja/imessage_buffer.json. This function
    reads that buffer instead of accessing the database directly, so
    the Python process does not need Full Disk Access.

    New contract (per the 2026-04 messaging unification): one Observation
    per speaker-turn. Buffer rows carry ``chat_id`` (stable chat.ROWID),
    ``chat_label`` (human name), and ``speaker`` (single participant for
    this turn). Legacy buffer rows without those fields get synthesized
    values from ``sender`` so mid-rollout the live app keeps working.
    """
    from deja.observations.contacts import resolve_contact, name_with_handle

    results: list[Observation] = []
    try:
        if not _IMESSAGE_BUFFER.exists():
            return results
        data = json.loads(_IMESSAGE_BUFFER.read_text())
        for r in data[:limit]:
            text = (r.get("text") or "")[:500]
            dt = r.get("dt", "")
            if not text:
                continue

            # --- new-shape fields (with legacy fallback) ---
            chat_id = r.get("chat_id") or None
            chat_label = r.get("chat_label") or None
            raw_speaker = r.get("speaker")

            # Legacy buffer: only `sender` is present. In that world the
            # "sender" is a raw handle for inbound 1:1 messages or "me"
            # for outbound. Synthesize chat_id from sender so thread
            # context still groups; speaker == sender (rewritten "You").
            if raw_speaker is None:
                legacy_sender = r.get("sender", "unknown")
                raw_speaker = legacy_sender
                if chat_id is None:
                    chat_id = f"imsg-legacy-{legacy_sender}"
                if chat_label is None:
                    chat_label = legacy_sender

            # Resolve chat_label if it's a raw handle (phone/email).
            # Swift writes chat_label as chat.display_name OR
            # chat_identifier; for 1:1s, display_name is usually empty
            # and chat_identifier is just a phone. Without this lookup,
            # integrate sees the phone as the "other party" and has no
            # grounding for who "Joe" is in "Hey Joe...". Fabrication
            # follows. Resolve via macOS Contacts so the recipient's
            # real name is in the signal.
            if chat_label and (
                chat_label.startswith("+")
                or (chat_label.replace("-", "").replace(" ", "").replace("(", "").replace(")", "").isdigit())
                or "@" in chat_label
            ):
                resolved_label = resolve_contact(chat_label)
                if resolved_label:
                    chat_label = name_with_handle(resolved_label, chat_label)

            # Resolve speaker's raw handle to a contact name when inbound.
            if raw_speaker == "me":
                speaker = "You"
            else:
                resolved = resolve_contact(raw_speaker) or raw_speaker
                if "+" in (raw_speaker or "") or "@" in (raw_speaker or ""):
                    speaker = name_with_handle(resolved, raw_speaker)
                else:
                    speaker = resolved

            # sender (display string) — chat label for groups, or the
            # speaker for 1:1 back-compat behavior. Downstream code that
            # still reads `sender` sees a stable per-thread identifier.
            sender = chat_label or speaker

            # Build a speaker-scoped id_key so two speakers in the same
            # chat at the same second hash distinctly.
            speaker_hash = hashlib.md5((raw_speaker or "").encode()).hexdigest()[:6]
            text_hash = hashlib.md5(text.encode()).hexdigest()[:12]
            id_key = f"imsg-{chat_id}-{speaker_hash}-{dt}-{text_hash}"

            try:
                ts = datetime.strptime(dt, "%Y-%m-%d %H:%M:%S")
            except (ValueError, TypeError):
                ts = datetime.now()

            if _is_self_chat_turn(raw_speaker, chat_label):
                _dispatch_self_imessage_to_cos(text=text, id_key=id_key, ts=ts)
                continue

            results.append(
                Observation(
                    source="imessage",
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
        from deja.observations.contacts import resolve_contact, name_with_handle

        for chat in chat_rows:
            chat_rowid = chat["chat_id"]
            stable_chat_id = f"imsg-chat-{chat_rowid}"
            is_group = (chat["style"] == 43)  # style 43 = group, 45 = 1:1
            display_name = (chat["display_name"] or "").strip()

            # Who's in this chat (excluding the user themselves — the
            # user is implicit). We still need this to compute chat_label
            # for groups without a display_name.
            handle_rows = conn.execute(
                """
                SELECT h.id
                FROM chat_handle_join chj
                JOIN handle h ON h.ROWID = chj.handle_id
                WHERE chj.chat_id = ?
                """,
                (chat_rowid,),
            ).fetchall()
            participant_handles = [h["id"] for h in handle_rows if h["id"]]
            participant_pairs: list[tuple[str, str]] = []
            for handle in participant_handles:
                resolved = resolve_contact(handle) or handle
                participant_pairs.append((resolved, handle))
            participant_names = [name for name, _ in participant_pairs]

            if is_group:
                if display_name:
                    chat_label = display_name
                else:
                    short_names = ", ".join(participant_names[:3])
                    extra = (
                        f" +{len(participant_names) - 3}"
                        if len(participant_names) > 3 else ""
                    )
                    chat_label = f"group ({short_names}{extra})"
            else:
                if participant_pairs:
                    name, handle = participant_pairs[0]
                    chat_label = name_with_handle(name, handle)
                else:
                    chat_label = "unknown"

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
                (chat_rowid, cutoff_apple_ns, messages_per_contact),
            ).fetchall()

            # Reverse so oldest-first (the DB returned newest-first to
            # apply LIMIT correctly).
            msg_rows = list(reversed(msg_rows))

            if not msg_rows:
                continue

            # Emit ONE observation per message. All share the same
            # chat_id so format-time thread reconstruction rebuilds the
            # conversation cleanly, and per-turn speaker attribution
            # prevents the downstream integrator from fusing speakers.
            for m in msg_rows:
                body = (m["text"] or "").replace("\n", " ").strip()
                if not body:
                    continue
                body = body[:500]

                if m["is_from_me"]:
                    speaker = "You"
                    raw_speaker = "me"
                else:
                    raw = m["sender_handle"] or "unknown"
                    resolved = resolve_contact(raw) or raw
                    speaker = name_with_handle(resolved, raw)
                    raw_speaker = raw

                try:
                    ts = datetime.strptime(m["dt"], "%Y-%m-%d %H:%M:%S")
                except (ValueError, TypeError, KeyError):
                    ts = datetime.now()

                speaker_hash = hashlib.md5(raw_speaker.encode()).hexdigest()[:6]
                text_hash = hashlib.md5(body.encode()).hexdigest()[:12]
                id_key = (
                    f"imsg-backfill-{chat_rowid}-{speaker_hash}-"
                    f"{m['date']}-{text_hash}"
                )

                results.append(Observation(
                    source="imessage",
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
            "iMessage backfill failed (likely Full Disk Access not granted): %s",
            e,
        )
        return []
    except Exception:
        log.exception("iMessage backfill failed with unexpected error")
        return []

    # Sort oldest → newest so batches show the timeline in order.
    results.sort(key=lambda o: o.timestamp)
    log.info("iMessage backfill: returning %d per-turn observations", len(results))
    return results
