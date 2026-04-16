"""Email signal collector using the gws CLI tool.

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
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

from deja.config import DEJA_HOME
from deja.observations.base import BaseObserver
from deja.observations.types import Observation

log = logging.getLogger(__name__)


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


def _bootstrap_cursor() -> str | None:
    """Bootstrap cursor from users.getProfile — returns current historyId."""
    try:
        result = subprocess.run(
            [
                "gws", "gmail", "users", "getProfile",
                "--params", json.dumps({"userId": "me"}),
                "--format", "json",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            log.error("gws gmail getProfile failed: %s", result.stderr[:200])
            return None
        data = json.loads(result.stdout)
        hid = str(data.get("historyId") or "")
        if not hid:
            log.error("gws gmail getProfile returned no historyId")
            return None
        _write_cursor_atomic(hid)
        log.info("Gmail history cursor bootstrapped at %s", hid)
        return hid
    except subprocess.TimeoutExpired:
        log.error("gws gmail getProfile timed out")
        return None
    except (json.JSONDecodeError, FileNotFoundError) as e:
        log.error("gws gmail getProfile failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# History delta fetch
# ---------------------------------------------------------------------------


def _list_history_since(start_history_id: str) -> tuple[list[dict], str | None]:
    """Page through users.history.list since ``start_history_id``.

    Returns (history_records, new_history_id). ``new_history_id`` is the
    most recent historyId we've seen — caller persists it as the next
    cursor. On failure returns ([], None) and caller will NOT advance.
    """
    records: list[dict] = []
    new_history_id: str | None = None
    page_token: str | None = None

    for _ in range(20):  # hard cap on pagination
        params: dict = {
            "userId": "me",
            "startHistoryId": start_history_id,
            # gws marshals this param as a string, not a JSON array.
            # Array form ("[messageAdded]") returns 400 "Invalid value
            # at 'history_types'". Tested 2026-04-12 on gws CLI.
            "historyTypes": "messageAdded",
        }
        if page_token:
            params["pageToken"] = page_token

        try:
            result = subprocess.run(
                [
                    "gws", "gmail", "users", "history", "list",
                    "--params", json.dumps(params),
                    "--format", "json",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            log.error("gws gmail history list timed out")
            return [], None
        except FileNotFoundError:
            log.error("gws CLI not found on PATH")
            return [], None

        if result.returncode != 0:
            stderr = result.stderr or ""
            # 404 → historyId too old (purged). Re-bootstrap from current.
            if "404" in stderr or "notFound" in stderr or "historyId" in stderr.lower():
                log.error("Gmail historyId expired; re-bootstrapping cursor")
                new = _bootstrap_cursor()
                return [], new
            log.error("gws gmail history list failed: %s", stderr[:200])
            return [], None

        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            log.error("gws gmail history list returned invalid JSON")
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
    try:
        result = subprocess.run(
            [
                "gws", "gmail", "users", "messages", "get",
                "--params", json.dumps({
                    "userId": "me",
                    "id": msg_id,
                    # metadataHeaders works on users.messages.get (unlike
                    # threads.get), but we use full for consistency and
                    # because bandwidth cost is negligible.
                    "format": "metadata",
                    "metadataHeaders": ["From", "To", "Subject", "Date"],
                }),
                "--format", "json",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            return None
        return json.loads(result.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        return None


def _get_thread(thread_id: str) -> list[dict]:
    """Fetch a thread.

    Uses ``format=full`` because Gmail's ``users.threads.get`` does NOT
    support the ``metadataHeaders`` parameter — it's a ``messages.get``
    feature only. Passing metadata+metadataHeaders to threads.get made
    Gmail silently ignore the filter and return a payload with zero
    headers, which then cascaded into every email being attributed to
    ``sender: "Unknown"`` and tier-3'd as noise.

    We fetch full messages but only read the handful of headers we need
    (From/To/Subject/Date) in _build_observation_from_thread. The body
    is discarded.
    """
    try:
        result = subprocess.run(
            [
                "gws", "gmail", "users", "threads", "get",
                "--params", json.dumps({
                    "userId": "me",
                    "id": thread_id,
                    "format": "full",
                }),
                "--format", "json",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            return []
        data = json.loads(result.stdout)
        return data.get("messages", []) or []
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        return []


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
    thread_lines: list[str] = []
    # True iff ANY message in this thread was sent by the user (has the
    # Gmail SENT label). An engaged thread — even an incoming-only reply
    # to it — is Tier 1 because the user has explicitly committed to the
    # conversation by responding before.
    user_replied_to_thread = False
    for tm in thread_messages:
        hdrs = {h.get("name"): h.get("value", "")
                for h in tm.get("payload", {}).get("headers", [])}
        if "SENT" in (tm.get("labelIds") or []):
            user_replied_to_thread = True
        if not subject:
            subject = hdrs.get("Subject", "")
        frm = hdrs.get("From", "")
        to = hdrs.get("To", "")
        date = hdrs.get("Date", "")
        snip = (tm.get("snippet") or "").replace("&#39;", "'").replace("&amp;", "&").replace("&quot;", '"')
        latest_from = frm or latest_from
        latest_to = to or latest_to
        latest_date = date or latest_date
        thread_lines.append(f"  {frm} → {to} ({date[:25]}): {snip[:250]}")

    if any(n.lower() in subject.lower() for n in _SYSTEM_NOISE):
        return None

    n = len(thread_messages)
    if n == 1:
        text = f"{subject} — {thread_lines[0].strip()}"
    else:
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
        text=text[:2000],
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
    """One page of gws gmail users messages list. Returns (messages, next_page_token)."""
    params: dict = {
        "userId": "me",
        "q": query,
        "maxResults": max_results,
    }
    if page_token:
        params["pageToken"] = page_token
    try:
        result = subprocess.run(
            [
                "gws", "gmail", "users", "messages", "list",
                "--params", json.dumps(params),
                "--format", "json",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            log.warning("gws gmail list failed (%s): %s", query, result.stderr[:200])
            return [], None
        data = json.loads(result.stdout)
        return data.get("messages", []) or [], data.get("nextPageToken")
    except subprocess.TimeoutExpired:
        log.warning("gws gmail list timed out (%s)", query)
        return [], None
    except json.JSONDecodeError:
        log.warning("gws gmail list returned invalid JSON (%s)", query)
        return [], None
    except FileNotFoundError:
        log.warning("gws CLI not found on PATH")
        return [], None


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
