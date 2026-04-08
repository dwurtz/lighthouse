"""Email signal collector using the gws CLI tool.

Collects from TWO streams so the agent sees both sides of every conversation:

1. Inbox (unread, recent) — incoming mail David hasn't read yet
2. Sent (recent)         — David's own outgoing mail

Without the sent stream, the agent only sees half of every exchange and can't
tell when David has committed to something, declined something, or closed a
loop. To: <person> subjects become first-class signals tagged as "David wrote
to X".
"""

from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime
from email.utils import parsedate_to_datetime

from deja.observations.base import BaseObserver
from deja.observations.types import Observation

log = logging.getLogger(__name__)


# Subject substrings that mean system-generated noise (not the user's real mail)
_SYSTEM_NOISE = [
    "[work]",
]


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


def _fetch_emails(
    query: str,
    max_results: int = 10,
    direction: str = "incoming",
    *,
    max_pages: int = 1,
    seen_threads: set[str] | None = None,
    use_actual_timestamp: bool = False,
) -> list[Observation]:
    """Run one gmail list+get query and return Signals.

    direction = "incoming" for inbox mail, "outgoing" for sent mail.
    For outgoing, sender is rewritten to "David Wurtz → <to>" so the LLM can
    tell at a glance who sent it and who received it.

    ``max_pages`` allows the caller to paginate through large result sets
    (the backfill/onboarding path needs this — a month of sent mail can
    easily exceed the 500-result per-page cap). Defaults to 1 so the
    steady-state 15-minute collector cycle behaves identically.

    ``seen_threads`` is an externally-owned set used to dedupe threads
    within a single logical fetch. A thread the user sent three messages
    in will come back three times in an ``in:sent`` query — we only want
    to build one Observation for it. Pass the same set across calls to
    share dedup state; pass ``None`` to dedupe only within this call.
    """
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

        # Dedup by thread — a sent query returns one row per sent message,
        # so a thread the user replied to 3 times appears 3 times. We only
        # want one Observation per thread.
        if thread_id:
            if thread_id in dedup:
                continue
            dedup.add(thread_id)

        try:
            # Fetch the FULL thread, not just the single message. Replies
            # without their prior context are useless for the analysis cycle
            # ("Yes that works" → works for what?). Always get the thread.
            if thread_id:
                thread_result = subprocess.run(
                    [
                        "gws", "gmail", "users", "threads", "get",
                        "--params", json.dumps({
                            "userId": "me",
                            "id": thread_id,
                            "format": "metadata",
                            "metadataHeaders": ["From", "To", "Subject", "Date"],
                        }),
                        "--format", "json",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                if thread_result.returncode != 0:
                    continue
                thread_data = json.loads(thread_result.stdout)
                thread_messages = thread_data.get("messages", []) or []
            else:
                thread_messages = []

            if not thread_messages:
                continue

            # Build contextual signal: the whole thread, most recent on bottom.
            subject = ""
            latest_from = ""
            latest_to = ""
            latest_date = ""
            thread_lines: list[str] = []
            for tm in thread_messages:
                hdrs = {h.get("name"): h.get("value", "")
                        for h in tm.get("payload", {}).get("headers", [])}
                if not subject:
                    subject = hdrs.get("Subject", "")
                frm = hdrs.get("From", "")
                to = hdrs.get("To", "")
                date = hdrs.get("Date", "")
                snip = (tm.get("snippet") or "").replace("&#39;", "'").replace("&amp;", "&").replace("&quot;", '"')
                # Track the latest message's from/to/date for sender labeling.
                # Messages come back oldest-first, so the last iteration wins.
                latest_from = frm or latest_from
                latest_to = to or latest_to
                latest_date = date or latest_date
                thread_lines.append(f"  {frm} → {to} ({date[:25]}): {snip[:250]}")

            # Drop system-generated noise by subject
            if any(n.lower() in subject.lower() for n in _SYSTEM_NOISE):
                continue

            n = len(thread_messages)
            if n == 1:
                # Single-message thread — concise form
                text = f"{subject} — {thread_lines[0].strip()}"
            else:
                # Multi-message thread — show the full exchange
                text = f"EMAIL THREAD ({n} messages) — {subject}\n" + "\n".join(thread_lines)

            if direction == "outgoing":
                sender_label = f"David Wurtz → {latest_to or 'unknown'}"
                text = f"[SENT] {text}"
                id_key = f"email-sent-{msg_id}"
            else:
                sender_label = latest_from or "Unknown"
                id_key = f"email-{msg_id}"

            ts = datetime.now()
            if use_actual_timestamp and latest_date:
                try:
                    parsed = parsedate_to_datetime(latest_date)
                    if parsed is not None:
                        # Normalize to naive local time to match the rest
                        # of the signals pipeline (which uses datetime.now()).
                        ts = parsed.astimezone().replace(tzinfo=None)
                except (TypeError, ValueError):
                    pass

            signals.append(Observation(
                source="email",
                sender=sender_label[:100],
                text=text[:2000],  # threads need more room than singles
                timestamp=ts,
                id_key=id_key,
            ))
        except Exception as e:
            log.debug("Failed to fetch email thread for %s: %s", msg_id, e)
            continue

    return signals


class EmailObserver(BaseObserver):
    """Collects recent incoming and outgoing emails via gws CLI."""

    def __init__(self, since_minutes: int = 15) -> None:
        self.since_minutes = since_minutes

    @property
    def name(self) -> str:
        return "Email"

    def collect(self) -> list[Observation]:
        return collect_recent_emails(since_minutes=self.since_minutes)


def collect_recent_emails(since_minutes: int = 15) -> list[Observation]:
    """Collect recent incoming and outgoing emails.

    - Incoming: unread mail from the last N minutes
    - Outgoing: sent mail from the last N minutes (always included — David's
      own messages are never marked unread, so we query sent independently)
    """
    signals: list[Observation] = []
    try:
        signals.extend(_fetch_emails(
            query=f"is:unread newer_than:{since_minutes}m",
            max_results=5,
            direction="incoming",
        ))
    except Exception:
        log.exception("Inbox fetch failed")

    try:
        signals.extend(_fetch_emails(
            query=f"in:sent newer_than:{since_minutes}m",
            max_results=5,
            direction="outgoing",
        ))
    except Exception:
        log.exception("Sent mail fetch failed")

    return signals


def fetch_sent_threads_backfill(days: int = 30, max_threads: int = 1000) -> list[Observation]:
    """Fetch every thread the user was active in over the last ``days`` days.

    Queries ``in:sent newer_than:{days}d`` and fetches the full thread for
    each hit. Because a single thread with N sent messages shows up N
    times in the list, results are deduped by threadId — one Observation
    per unique thread. Timestamps are pulled from the latest message's
    Date header (not ``datetime.now()``), so the onboarding LLM sees when
    each conversation actually happened.

    This is the onboarding / cold-start entry point. The steady-state
    15-minute collector still uses ``collect_recent_emails``.

    ``max_threads`` caps the total number of observations returned across
    all pages — a safety valve for users with extreme email volume.
    """
    seen_threads: set[str] = set()
    # 500 is Gmail's per-page maximum. Walk up to enough pages to cover
    # the cap; the loop below trims to exactly max_threads after dedup.
    pages = max(1, (max_threads // 500) + 1)
    observations = _fetch_emails(
        query=f"in:sent newer_than:{days}d",
        max_results=500,
        direction="outgoing",
        max_pages=pages,
        seen_threads=seen_threads,
        use_actual_timestamp=True,
    )
    if len(observations) > max_threads:
        observations = observations[:max_threads]
    # Sort oldest → newest so the LLM sees the timeline in order when we
    # batch them. The Gmail list API returns newest-first.
    observations.sort(key=lambda o: o.timestamp)
    return observations
