"""Email signal collector using direct Gmail API calls.

Uses Gmail's delta API (`users.history.list`) keyed off a persisted
``historyId`` cursor so we only fetch messages that appeared since the
last poll. This makes the steady-state collector O(new-messages) instead
of re-scanning a rolling 5-minute window every cycle.

  * Steady-state (this module's ``EmailObserver``) — history.list delta
  * Onboarding backfill — still uses ``fetch_sent_threads_backfill``
    (``in:sent newer_than:{days}d``) because the whole point of onboarding
    is to pull historical mail.

Cursor persistence: ``~/.deja/gmail_history_cursor.txt``. On first run
(or corrupted/missing file) we bootstrap via ``users.getProfile`` which
returns the current ``historyId`` and we START there — we do NOT
backfill. That's onboarding's job, not the collector's.

Per the user's rule: if a delta call fails we log.error and return []
— no silent fallback to snapshot polling.

Transport: direct ``googleapiclient`` calls against the user's OAuth
token (collected by ``deja.auth`` at setup). Historically this module
shelled out to the ``gws`` CLI; see ``deja.google_api`` for the thin
helper that replaces it. The returned JSON shapes are identical —
``gws`` was always a thin passthrough.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
import tempfile
from datetime import datetime
from email.utils import parsedate_to_datetime

from deja.config import DEJA_HOME
from deja.observations.base import BaseObserver
from deja.observations.types import Observation

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Body extraction, quote stripping, consolidation
# ---------------------------------------------------------------------------

# Hard ceiling on a single extracted message body before quote-stripping.
# Marketing newsletters can be 50k+ chars of HTML-entity-encoded fluff; we
# don't want to feed those to integrate verbatim. If the body is obviously
# huge we trust snippet + truncation to handle it.
_BODY_CHAR_CAP = 6000

# Cap on the final rendered thread text stored on the Observation. Bumped
# up from the old 2000 (snippet-era) since full bodies legitimately need
# more room. Consolidation kicks in well before this, so most threads
# land well under the cap.
_RENDERED_THREAD_CAP = 12000

# Consolidation triggers. Either condition flips it on.
_CONSOLIDATE_MIN_MESSAGES = 10
_CONSOLIDATE_MIN_CHARS = 8000
# When consolidating, keep this many most-recent messages verbatim.
_CONSOLIDATE_KEEP_RECENT = 4

# Model for consolidation — same cheap tier used for screenshot preprocess.
_CONSOLIDATE_MODEL = "gemini-2.5-flash-lite"

# Quote-reply divider patterns. First line matching any of these (or the
# start of a contiguous ">"-prefixed block) cuts off quoted history.
_QUOTE_DIVIDER_RES = [
    re.compile(r"^On .+ wrote:\s*$"),
    re.compile(r"^On .+,\s*$"),  # multi-line "On <date>,\n<name> wrote:"
    re.compile(r"^-+\s*Original Message\s*-+\s*$", re.IGNORECASE),
    re.compile(r"^-+\s*Forwarded message\s*-+\s*$", re.IGNORECASE),
    re.compile(r"^Begin forwarded message:\s*$", re.IGNORECASE),
    re.compile(r"^_{5,}\s*$"),  # Outlook's underscore divider
    re.compile(r"^From:\s.+<.+@.+>\s*$"),  # Outlook inline quote header
]


def _decode_body_data(data: str) -> str:
    """Base64url-decode a Gmail payload body.data blob into UTF-8 text."""
    try:
        raw = base64.urlsafe_b64decode(data + "==")  # padding tolerance
        return raw.decode("utf-8", errors="replace")
    except Exception:
        return ""


def _extract_plain_text_body(payload: dict) -> str:
    """Walk a Gmail message payload and return the best plain-text body.

    Preference order:
      1. First ``text/plain`` part anywhere in the tree
      2. Top-level body.data if the message is itself text/plain
      3. Empty string — caller should fall back to snippet

    HTML-only emails return "" here; that's fine — we fall back to the
    250-char snippet via the caller, which is the same quality as the
    pre-change behavior for that (rare, marketing-heavy) case.
    """
    if not payload:
        return ""

    # Walk breadth-first so we prefer shallower text/plain parts.
    stack: list[dict] = [payload]
    while stack:
        node = stack.pop(0)
        mime = node.get("mimeType", "") or ""
        body = node.get("body") or {}
        data = body.get("data")
        if mime == "text/plain" and data:
            text = _decode_body_data(data)
            if text.strip():
                return text
        for child in node.get("parts", []) or []:
            stack.append(child)

    # No text/plain anywhere — last chance: maybe the top-level payload is
    # text/plain with its body stored directly.
    top_mime = payload.get("mimeType", "") or ""
    top_data = (payload.get("body") or {}).get("data")
    if top_mime.startswith("text/") and top_data:
        return _decode_body_data(top_data)

    return ""


def _strip_quoted_reply(body: str) -> str:
    """Cut the quoted-history tail off a reply body.

    Pragmatic: scan top-to-bottom for the first divider line. Everything
    from that line onward is quoted history. If nothing matches, return
    the body as-is (rather than over-aggressively trimming).

    Also strips the standard ``-- \\n`` signature delimiter when present,
    and collapses trailing whitespace.
    """
    if not body:
        return body

    lines = body.splitlines()
    cut_at: int | None = None

    for i, ln in enumerate(lines):
        stripped = ln.rstrip()

        # Contiguous ">"-prefixed block: mark first such line as the cut.
        if stripped.startswith(">"):
            # Only cut if the PREVIOUS line was blank or the start of
            # message — otherwise an inline quote inside the author's
            # own text would eat their response. Gmail-style replies put
            # a blank line + "On <date>..." header before the ">" block.
            if i == 0 or not lines[i - 1].strip():
                cut_at = i
                break
            # else: leave it and keep scanning — might hit a divider below.

        for pat in _QUOTE_DIVIDER_RES:
            if pat.match(stripped):
                cut_at = i
                break
        if cut_at is not None:
            break

    if cut_at is not None:
        lines = lines[:cut_at]

    # Drop trailing blank lines.
    while lines and not lines[-1].strip():
        lines.pop()

    # Strip standard signature delimiter ("-- " on its own line).
    for i, ln in enumerate(lines):
        if ln.rstrip() == "--" or ln == "-- ":
            lines = lines[:i]
            break

    result = "\n".join(lines).rstrip()
    # Guard against degenerate results — if stripping leaves us with
    # almost nothing, prefer the original body (rare bottom-posted reply
    # or a malformed divider match). Trigger on threshold ratio: if <10%
    # of the original body survived, assume the cut was wrong.
    if cut_at is not None and len(result) < 20 and len(body.strip()) > 40:
        return body.rstrip()
    return result


def _local_date_string(raw_date: str) -> str:
    """Convert an RFC 2822 Date header to local ``YYYY-MM-DD HH:MM``.

    Gmail returns dates in the sender's timezone (often UTC for server
    relays). We render per-message thread lines to the integrate prompt;
    if we leave the raw header in place, the LLM will copy e.g. a UTC
    ``14:55`` into an event's ``time:`` field when the local time was
    ``07:55``. Normalize to the user's local zone so the prompt shows
    one consistent clock.
    """
    if not raw_date:
        return ""
    try:
        parsed = parsedate_to_datetime(raw_date)
        if parsed is None:
            return raw_date[:25]
        local = parsed.astimezone().replace(tzinfo=None)
        return local.strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return raw_date[:25]


def _render_messages_verbatim(messages: list[dict]) -> list[str]:
    """Render a list of (headers, stripped_body) dicts to thread lines.

    Each input element is the shape ``{"frm", "to", "date", "body"}``.
    Output lines match the legacy indent/format used by downstream
    integrate prompts.
    """
    lines: list[str] = []
    for m in messages:
        frm = m.get("frm", "")
        to = m.get("to", "")
        date = _local_date_string(m.get("date") or "")
        body = (m.get("body") or "").strip()
        if body:
            # Indent body under the header line for readability.
            body_block = "\n    ".join(body.splitlines())
            lines.append(f"  {frm} → {to} ({date}):\n    {body_block}")
        else:
            # Body empty (e.g. HTML-only) — fall back to the snippet
            # supplied by the caller.
            snip = m.get("snippet", "")
            lines.append(f"  {frm} → {to} ({date}): {snip}")
    return lines


def _consolidate_older_history(older: list[dict]) -> str | None:
    """One-paragraph Gemini Flash-Lite summary of older thread messages.

    Returns None on any failure — caller falls back to verbatim rendering.
    """
    if not older:
        return None

    # Build the text we're feeding the model.
    rendered = "\n\n".join(
        f"From: {m.get('frm','')}\nTo: {m.get('to','')}\nDate: {_local_date_string(m.get('date') or '')}\n\n{(m.get('body') or '').strip()}"
        for m in older
    )

    prompt = (
        "Summarize this email thread history in ONE paragraph. Focus on "
        "commitments, decisions, people, and dates. Omit pleasantries, "
        "greetings, and signatures. Keep it terse — under 300 tokens.\n\n"
        "Thread messages (oldest first):\n\n"
        + rendered
    )

    try:
        from deja.llm_client import GeminiClient
    except Exception:
        log.warning("email consolidate: GeminiClient import failed", exc_info=True)
        return None

    async def _run() -> str:
        client = GeminiClient()
        return await asyncio.wait_for(
            client._generate(
                model=_CONSOLIDATE_MODEL,
                contents=prompt,
                config_dict={
                    "max_output_tokens": 400,
                    "temperature": 0.0,
                },
            ),
            timeout=15.0,
        )

    try:
        summary = asyncio.run(_run())
    except RuntimeError as e:
        # asyncio.run blew up because we're already in a loop. That
        # shouldn't happen from the sync collector, but log and bail
        # rather than crashing the cycle.
        log.warning("email consolidate: asyncio.run failed: %s", e)
        return None
    except asyncio.TimeoutError:
        log.warning("email consolidate: timed out after 15s")
        return None
    except Exception:
        log.warning("email consolidate: LLM call failed", exc_info=True)
        return None

    summary = (summary or "").strip()
    if not summary:
        return None
    return summary


# Subject substrings that mean system-generated noise (not the user's real mail)
_SYSTEM_NOISE = [
    "[work]",
]

_CURSOR_PATH = DEJA_HOME / "gmail_history_cursor.txt"


# ---------------------------------------------------------------------------
# Cursor helpers
# ---------------------------------------------------------------------------


def _read_cursor() -> str | None:
    try:
        if not _CURSOR_PATH.exists():
            return None
        raw = _CURSOR_PATH.read_text().strip()
        if not raw:
            return None
        # Validate — Gmail historyId is always a decimal string
        int(raw)
        return raw
    except (OSError, ValueError):
        log.warning("Gmail history cursor unreadable; will bootstrap")
        return None


def _write_cursor_atomic(history_id: str) -> None:
    try:
        DEJA_HOME.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".gmail_cursor.", dir=str(DEJA_HOME))
        try:
            with os.fdopen(fd, "w") as f:
                f.write(str(history_id))
            os.replace(tmp, _CURSOR_PATH)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception:
        log.exception("Failed to persist Gmail history cursor")


def _gmail_service():
    """Return the cached Gmail API service, or None on auth failure.

    Wraps ``deja.google_api.get_service`` with a broad try/except so
    the collector degrades the same way the old gws path did —
    log.error + return [] rather than propagating an exception up
    into the agent loop.
    """
    try:
        from deja.google_api import get_service
        return get_service("gmail", "v1")
    except Exception:
        log.error("Gmail service unavailable — is setup complete?", exc_info=True)
        return None


def _bootstrap_cursor() -> str | None:
    """Bootstrap cursor from users.getProfile — returns current historyId."""
    svc = _gmail_service()
    if svc is None:
        return None
    try:
        data = svc.users().getProfile(userId="me").execute()
    except Exception as e:
        log.error("Gmail getProfile failed: %s", type(e).__name__)
        return None
    hid = str(data.get("historyId") or "")
    if not hid:
        log.error("Gmail getProfile returned no historyId")
        return None
    _write_cursor_atomic(hid)
    log.info("Gmail history cursor bootstrapped at %s", hid)
    return hid


# ---------------------------------------------------------------------------
# History delta fetch
# ---------------------------------------------------------------------------


def _list_history_since(start_history_id: str) -> tuple[list[dict], str | None]:
    """Page through users.history.list since ``start_history_id``.

    Returns (history_records, new_history_id). ``new_history_id`` is the
    most recent historyId we've seen — caller persists it as the next
    cursor. On failure returns ([], None) and caller will NOT advance.

    Note: direct Gmail API accepts ``historyTypes`` as a list —
    unlike the gws CLI which required a string. The param shape is
    the native one now.
    """
    svc = _gmail_service()
    if svc is None:
        return [], None

    records: list[dict] = []
    new_history_id: str | None = None
    page_token: str | None = None

    for _ in range(20):  # hard cap on pagination
        try:
            req = svc.users().history().list(
                userId="me",
                startHistoryId=start_history_id,
                historyTypes=["messageAdded"],
                pageToken=page_token,
            )
            data = req.execute()
        except Exception as e:
            # googleapiclient.errors.HttpError has a ``resp.status`` attr.
            status = getattr(getattr(e, "resp", None), "status", None)
            # 404 → historyId too old (purged). Re-bootstrap from current.
            if status == 404:
                log.error("Gmail historyId expired; re-bootstrapping cursor")
                new = _bootstrap_cursor()
                return [], new
            log.error("Gmail history list failed: %s (status=%s)",
                      type(e).__name__, status)
            return [], None

        batch = data.get("history", []) or []
        records.extend(batch)
        hid = data.get("historyId")
        if hid:
            new_history_id = str(hid)
        page_token = data.get("nextPageToken")
        if not page_token:
            break

    return records, new_history_id


def _get_message_metadata(msg_id: str) -> dict | None:
    """Fetch a single message (metadata format) for thread lookup."""
    svc = _gmail_service()
    if svc is None:
        return None
    try:
        return svc.users().messages().get(
            userId="me",
            id=msg_id,
            # metadataHeaders works on users.messages.get (unlike
            # threads.get), but we use full for consistency and
            # because bandwidth cost is negligible.
            format="metadata",
            metadataHeaders=["From", "To", "Subject", "Date"],
        ).execute()
    except Exception:
        return None


def _get_thread(thread_id: str) -> list[dict]:
    """Fetch a thread.

    Uses ``format=full`` because Gmail's ``users.threads.get`` does NOT
    support the ``metadataHeaders`` parameter — it's a ``messages.get``
    feature only. Passing metadata+metadataHeaders to threads.get made
    Gmail silently ignore the filter and return a payload with zero
    headers, which then cascaded into every email being attributed to
    ``sender: "Unknown"`` and tier-3'd as noise.

    We fetch full messages so _build_observation_from_thread can read
    the headers (From/To/Subject/Date) AND extract the plain-text body
    for quote-stripping + consolidation.
    """
    svc = _gmail_service()
    if svc is None:
        return []
    try:
        data = svc.users().threads().get(
            userId="me",
            id=thread_id,
            format="full",
        ).execute()
    except Exception:
        return []
    return data.get("messages", []) or []


def _build_observation_from_thread(
    msg_id: str,
    thread_messages: list[dict],
    direction: str,
) -> Observation | None:
    """Build a single Observation from a thread's messages.

    Mirrors the structure used by the old snapshot collector so downstream
    consumers (prefilter, integrate) don't notice the switch.
    """
    if not thread_messages:
        return None

    subject = ""
    latest_from = ""
    latest_to = ""
    latest_date = ""
    # Per-message structured records — used for rendering and (when the
    # thread is long) consolidation. Each record keeps the full stripped
    # body so the consolidator has the real content, not just the snippet.
    per_msg: list[dict] = []
    # True iff ANY message in this thread was sent by the user (has the
    # Gmail SENT label). An engaged thread — even an incoming-only reply
    # to it — is Tier 1 because the user has explicitly committed to the
    # conversation by responding before.
    user_replied_to_thread = False
    for tm in thread_messages:
        payload = tm.get("payload", {}) or {}
        hdrs = {h.get("name"): h.get("value", "")
                for h in payload.get("headers", [])}
        if "SENT" in (tm.get("labelIds") or []):
            user_replied_to_thread = True
        if not subject:
            subject = hdrs.get("Subject", "")
        frm = hdrs.get("From", "")
        to = hdrs.get("To", "")
        date = hdrs.get("Date", "")
        snip = (tm.get("snippet") or "").replace("&#39;", "'").replace("&amp;", "&").replace("&quot;", '"')

        # Full plain-text body, stripped of quoted history. Fall back to
        # the snippet only when extraction yields nothing.
        body_raw = _extract_plain_text_body(payload)
        if len(body_raw) > _BODY_CHAR_CAP:
            body_raw = body_raw[:_BODY_CHAR_CAP]
        body_clean = _strip_quoted_reply(body_raw).strip() if body_raw else ""
        if not body_clean:
            body_clean = snip[:250]

        latest_from = frm or latest_from
        latest_to = to or latest_to
        latest_date = date or latest_date
        per_msg.append({
            "frm": frm,
            "to": to,
            "date": date,
            "body": body_clean,
            "snippet": snip[:250],
        })

    if any(n.lower() in subject.lower() for n in _SYSTEM_NOISE):
        return None

    # Filter [Deja]-prefixed self-emails — those are cos notifications
    # written BY the agent TO the user. If we let them through, they'd
    # appear as [SENT] outbound signals, integrate would write events
    # about them, cos on the next cycle would re-email about those
    # events, and the loop never converges. This filter cuts the loop
    # at the observer layer. Only affects emails where the user is
    # both sender and recipient AND the subject is [Deja]-prefixed —
    # so legitimate self-notes stay in.
    try:
        from deja.identity import load_user
        user_email = (load_user().email or "").lower()
    except Exception:
        user_email = ""
    if user_email and subject.strip().startswith("[Deja]"):
        from_lower = (latest_from or "").lower()
        to_lower = (latest_to or "").lower()
        if user_email in from_lower and user_email in to_lower:
            log.debug("email: skipping cos self-email loopback: %s", subject[:60])
            return None

    n = len(thread_messages)

    if n == 1:
        # Single message path — just render body under a header line.
        m = per_msg[0]
        body = (m.get("body") or "").strip()
        header = f"{m['frm']} → {m['to']} ({(m.get('date') or '')[:25]})"
        if body:
            text = f"{subject} — {header}\n    " + "\n    ".join(body.splitlines())
        else:
            text = f"{subject} — {header}: {m.get('snippet','')}"
    else:
        # Multi-message thread. Decide whether to consolidate.
        total_body_chars = sum(len((m.get("body") or "")) for m in per_msg)
        should_consolidate = (
            n >= _CONSOLIDATE_MIN_MESSAGES
            or total_body_chars >= _CONSOLIDATE_MIN_CHARS
        )

        if should_consolidate:
            keep = min(_CONSOLIDATE_KEEP_RECENT, max(1, n - 1))
            older = per_msg[:-keep]
            recent = per_msg[-keep:]
            summary = _consolidate_older_history(older)
            if summary:
                recent_lines = _render_messages_verbatim(recent)
                text = (
                    f"EMAIL THREAD ({n} messages) — {subject}\n"
                    f"## Earlier in this thread ({len(older)} messages summarized)\n"
                    f"{summary}\n\n"
                    f"## Recent messages ({len(recent)} verbatim)\n"
                    + "\n".join(recent_lines)
                )
            else:
                # Consolidation failed — fall back to full verbatim render
                # rather than dropping the signal.
                log.info(
                    "email: consolidation unavailable for %d-msg thread, rendering verbatim",
                    n,
                )
                thread_lines = _render_messages_verbatim(per_msg)
                text = f"EMAIL THREAD ({n} messages) — {subject}\n" + "\n".join(thread_lines)
        else:
            thread_lines = _render_messages_verbatim(per_msg)
            text = f"EMAIL THREAD ({n} messages) — {subject}\n" + "\n".join(thread_lines)

    if direction == "outgoing":
        # Direction comes from Gmail's SENT label on the message — that's
        # the ground truth, not anything we stamp in frontmatter.
        try:
            from deja.identity import load_user
            user_name = load_user().name or "You"
        except Exception:
            user_name = "You"
        sender_label = f"{user_name} → {latest_to or 'unknown'}"
        text = f"[SENT] {text}"
        id_key = f"email-sent-{msg_id}"
    else:
        sender_label = latest_from or "Unknown"
        id_key = f"email-{msg_id}"
        # Stamp [ENGAGED] on incoming messages in threads the user has
        # participated in — they're Tier 1 by the "you replied, you care"
        # rule. The tier classifier keys off this prefix.
        if user_replied_to_thread:
            text = f"[ENGAGED] {text}"

    ts = datetime.now()
    if latest_date:
        try:
            parsed = parsedate_to_datetime(latest_date)
            if parsed is not None:
                ts = parsed.astimezone().replace(tzinfo=None)
        except (TypeError, ValueError):
            pass

    return Observation(
        source="email",
        sender=sender_label[:100],
        text=text[:_RENDERED_THREAD_CAP],
        timestamp=ts,
        id_key=id_key,
    )


def _collect_via_history(cursor: str) -> tuple[list[Observation], str | None]:
    """Run one delta pass. Returns (observations, new_cursor_to_persist)."""
    records, new_cursor = _list_history_since(cursor)
    if not records and new_cursor is None:
        # Hard failure — don't advance cursor
        return [], None

    # Collect unique (message_id, thread_id, labels) tuples from all
    # messageAdded entries across all history records.
    seen_msgs: dict[str, tuple[str, list[str]]] = {}
    for rec in records:
        for added in rec.get("messagesAdded", []) or []:
            m = added.get("message") or {}
            mid = m.get("id")
            if not mid or mid in seen_msgs:
                continue
            tid = m.get("threadId") or ""
            labels = m.get("labelIds", []) or []
            seen_msgs[mid] = (tid, labels)

    # Dedup by thread — one Observation per thread, keyed to the latest
    # triggering message id so id_key stays stable.
    by_thread: dict[str, tuple[str, list[str]]] = {}
    for mid, (tid, labels) in seen_msgs.items():
        if not tid:
            # No thread id → treat message as its own thread
            by_thread[mid] = (mid, labels)
        else:
            by_thread[tid] = (mid, labels)

    observations: list[Observation] = []
    for tid, (mid, labels) in by_thread.items():
        # Determine direction from labels
        direction = "outgoing" if "SENT" in labels else "incoming"
        # Fetch thread for context
        if tid != mid:
            thread_messages = _get_thread(tid)
        else:
            # No proper thread id — fetch message solo
            msg = _get_message_metadata(mid)
            thread_messages = [msg] if msg else []
        if not thread_messages:
            continue
        obs = _build_observation_from_thread(mid, thread_messages, direction)
        if obs:
            observations.append(obs)

    return observations, new_cursor


# ---------------------------------------------------------------------------
# Observer
# ---------------------------------------------------------------------------


class EmailObserver(BaseObserver):
    """Delta-based Gmail collector via users.history.list."""

    def __init__(self, since_minutes: int = 15) -> None:
        # ``since_minutes`` retained for backwards compatibility with callers
        # constructing this observer, but it's no longer used — delta polling
        # doesn't need a lookback window.
        self.since_minutes = since_minutes

    @property
    def name(self) -> str:
        return "Email"

    def collect(self) -> list[Observation]:
        cursor = _read_cursor()
        if cursor is None:
            cursor = _bootstrap_cursor()
            if cursor is None:
                log.error("Gmail collector: no cursor available, skipping cycle")
                return []
            # Freshly bootstrapped — don't backfill, just return empty and
            # pick up deltas next cycle.
            return []

        observations, new_cursor = _collect_via_history(cursor)
        if new_cursor:
            _write_cursor_atomic(new_cursor)
        return observations


# ---------------------------------------------------------------------------
# Onboarding backfill (unchanged — still uses snapshot query)
# ---------------------------------------------------------------------------


def _list_messages(query: str, max_results: int, page_token: str | None = None) -> tuple[list[dict], str | None]:
    """One page of gmail users messages list. Returns (messages, next_page_token)."""
    svc = _gmail_service()
    if svc is None:
        return [], None
    try:
        data = svc.users().messages().list(
            userId="me",
            q=query,
            maxResults=max_results,
            pageToken=page_token,
        ).execute()
    except Exception as e:
        log.warning("gmail messages.list failed (%s): %s", query, type(e).__name__)
        return [], None
    return data.get("messages", []) or [], data.get("nextPageToken")


def _fetch_emails_backfill(
    query: str,
    max_results: int = 10,
    direction: str = "outgoing",
    *,
    max_pages: int = 1,
    seen_threads: set[str] | None = None,
) -> list[Observation]:
    """Snapshot fetch used only by ``fetch_sent_threads_backfill``."""
    signals: list[Observation] = []
    dedup: set[str] = seen_threads if seen_threads is not None else set()

    messages: list[dict] = []
    page_token: str | None = None
    for _ in range(max(1, max_pages)):
        batch, page_token = _list_messages(query, max_results, page_token)
        messages.extend(batch)
        if not page_token:
            break

    for msg in messages:
        msg_id = msg.get("id")
        thread_id = msg.get("threadId")
        if not msg_id:
            continue
        if thread_id:
            if thread_id in dedup:
                continue
            dedup.add(thread_id)

        try:
            thread_messages = _get_thread(thread_id) if thread_id else []
            if not thread_messages:
                continue
            obs = _build_observation_from_thread(msg_id, thread_messages, direction)
            if obs:
                signals.append(obs)
        except Exception as e:
            log.debug("Failed to fetch email thread for %s: %s", msg_id, e)
            continue

    return signals


def fetch_sent_threads_backfill(days: int = 30, max_threads: int = 1000) -> list[Observation]:
    """Fetch every thread the user was active in over the last ``days`` days.

    Onboarding / cold-start entry point only. Steady-state collection
    uses delta polling via ``EmailObserver``.
    """
    seen_threads: set[str] = set()
    pages = max(1, (max_threads // 500) + 1)
    observations = _fetch_emails_backfill(
        query=f"in:sent newer_than:{days}d",
        max_results=500,
        direction="outgoing",
        max_pages=pages,
        seen_threads=seen_threads,
    )
    if len(observations) > max_threads:
        observations = observations[:max_threads]
    observations.sort(key=lambda o: o.timestamp)
    return observations


# Backwards-compat shim for any callers still importing the old name.
def collect_recent_emails(since_minutes: int = 15) -> list[Observation]:
    """Deprecated: use ``EmailObserver().collect()``. Delegates to delta path."""
    return EmailObserver(since_minutes=since_minutes).collect()
