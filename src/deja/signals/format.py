"""Chronological timeline formatter for the integrate prompt.

The integrate LLM gets one batch of observations per cycle. Rather
than grouping them by tier (which loses temporal adjacency — the
cause-effect link between a sent email and a reply five minutes
later), this formatter renders them as a single chronological
timeline with per-line tier markers.

- [T1] = user-authored or inner-circle inbound (the anchors — every
  wiki update must trace back to at least one of these).
- [T2] = focused attention (a thread the user opened, a doc they
  dwelled on).
- [T3] = ambient (inbox views, notifications, passing screenshots) —
  corroborate only.

The markers are a **contract** between this formatter and the
integrate prompt. If you change them here, update
``default_assets/prompts/integrate.md`` to match.

For iMessage / WhatsApp signals — which arrive as per-message rows
from Swift — we inject the last N messages of the same thread as
``## Context`` before rendering ``## New this cycle``. That stops
Mike's "Sent" from landing naked when his prior message was in a
previous cycle. The history is reconstructed from
``~/.deja/observations.jsonl`` (append-only, persistent). See
``_inject_thread_context`` for the mechanics.

No per-signal truncation — Flash has a 1M token context window and
the signal text is whatever the collector captured.
"""

from __future__ import annotations

import json
import logging
from typing import Iterable

from deja.config import OBSERVATIONS_LOG
from deja.signals.tiering import classify_tier

log = logging.getLogger(__name__)


_TIER_MARKERS = {1: "[T1]", 2: "[T2]", 3: "[T3]"}

# Sources that get thread-context injection. Per-message buffers need
# this because each message is emitted as its own observation. Email
# already bundles the full thread at collection time (see
# observations/email.py), so we don't re-inject here.
_THREADED_SOURCES = {"imessage", "whatsapp"}

# How many prior messages per thread to surface as ``## Context``.
# 30 covers nearly all "chopped mid-conversation" cases without
# overwhelming integrate's prompt. The conversation body text is
# already capped at 500 chars per message in the collectors, so 30
# messages is ~15KB in the worst case — trivial at Flash's 1M window.
_THREAD_CONTEXT_LIMIT = 30


def _thread_key(obs: dict) -> tuple[str, str] | None:
    """Return the canonical thread identifier for a message signal, or None.

    For iMessage / WhatsApp, ``sender`` is the chat identifier — 1:1s
    use the contact's name + handle, groups include every participant
    phone in the sender string. Either way, two messages belong to the
    same thread iff their (source, sender) pair matches exactly.
    """
    source = (obs.get("source") or "").lower()
    if source not in _THREADED_SOURCES:
        return None
    sender = obs.get("sender") or ""
    if not sender:
        return None
    return (source, sender)


def _load_thread_context(
    key: tuple[str, str],
    exclude_id: str,
    limit: int,
) -> list[dict]:
    """Return up to ``limit`` prior messages in the thread identified by
    ``key``, in chronological order (oldest first).

    Scans ``observations.jsonl`` from the end backward — file is
    append-only so recent messages are at the tail. Stops early once
    we've collected ``limit`` matches. Excludes any observation whose
    ``id_key`` matches ``exclude_id`` (usually the current signal
    itself).
    """
    if not OBSERVATIONS_LOG.exists():
        return []

    # Read the entire file once per call. At current sizes (~20MB)
    # this is ~50-100ms; good enough while batches are small. If the
    # file grows past ~100MB, switch to a tail-seeking reader.
    try:
        with OBSERVATIONS_LOG.open(encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        log.debug("could not read %s for thread context", OBSERVATIONS_LOG, exc_info=True)
        return []

    hits: list[dict] = []
    want_source, want_sender = key
    for line in reversed(lines):
        if not line.strip():
            continue
        # Cheap substring pre-filter — avoid JSON-parsing lines that
        # obviously can't be relevant. The sender field is the most
        # selective; bail if it's not present in the raw bytes.
        if want_sender not in line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        if (d.get("source") or "").lower() != want_source:
            continue
        if (d.get("sender") or "") != want_sender:
            continue
        if d.get("id_key") == exclude_id:
            continue
        hits.append(d)
        if len(hits) >= limit:
            break

    hits.reverse()  # oldest first
    return hits


import re


_CONVERSATION_HEADER_RE = re.compile(
    r"^CONVERSATION with .+?\(\d+ messages?, [0-9:\-]+\):\s*\n", re.MULTILINE
)


def _extract_messages(obs: dict) -> list[tuple[str, str]]:
    """Return a list of ``(sender, body)`` pairs from one observation.

    iMessage / WhatsApp observations arrive in two shapes:
    - Single-message rows from the live buffer: text is just the
      message body; ``sender`` is the chat identifier.
    - Cumulative CONVERSATION digests from some legacy paths: text
      starts with ``CONVERSATION with <chat> (N messages, ...):``
      followed by indented ``  <sender>: <body>`` lines.

    We peel the digest into individual messages when present, so the
    downstream context dedup can match on message content regardless
    of which shape the observation stored.
    """
    text = (obs.get("text") or "").strip()
    if not text:
        return []

    if _CONVERSATION_HEADER_RE.match(text):
        # Drop the header line and parse the indented message list.
        lines = text.split("\n")[1:]
        pairs: list[tuple[str, str]] = []
        for raw in lines:
            line = raw.strip()
            if not line or ":" not in line:
                continue
            who, _, body = line.partition(":")
            pairs.append((who.strip(), body.strip()))
        return pairs

    # Single-message row. Sender line carries the chat identifier;
    # body attribution (who in a group) is baked into the text itself
    # on the Swift side. Use a synthetic sender of empty string so the
    # dedup key is body-only and cross-shape dedup still works.
    return [("", text)]


def _inject_thread_context(obs: dict) -> dict:
    """Return a copy of ``obs`` with ``## Context`` / ``## New``
    sections prepended to its text when it's a threaded source.

    If the source isn't threaded, or no prior messages exist, returns
    the observation unchanged.
    """
    key = _thread_key(obs)
    if key is None:
        return obs

    prior_obs = _load_thread_context(
        key, exclude_id=obs.get("id_key") or "", limit=_THREAD_CONTEXT_LIMIT
    )
    if not prior_obs:
        return obs

    # Walk prior observations oldest → newest, flatten each into
    # individual messages, dedup by body text. Overlapping digests
    # collapse to one entry per distinct message, keeping the
    # earliest-seen timestamp for ordering.
    seen_bodies: set[str] = set()
    messages: list[tuple[str, str, str]] = []  # (ts, sender, body)
    for p in prior_obs:
        ts = (p.get("timestamp") or "")[:16]
        for sender, body in _extract_messages(p):
            # Normalize for dedup — collapse whitespace, cap length
            # so minor trailing-space differences don't split dups.
            key_body = " ".join(body.split())[:240]
            if not key_body or key_body in seen_bodies:
                continue
            seen_bodies.add(key_body)
            messages.append((ts, sender, body))

    # Also dedup current observation's body from the context — if the
    # current signal's text already appears in history (it will, for
    # the CONVERSATION-digest path where "current" is a 1-message
    # extract of a digest we saw before), we drop it from context to
    # avoid double-rendering.
    current_key = " ".join((obs.get("text") or "").split())[:240]
    messages = [m for m in messages if " ".join(m[2].split())[:240] != current_key]

    if not messages:
        return obs

    def _render(m: tuple[str, str, str]) -> str:
        ts, sender, body = m
        who = f"{sender}: " if sender else ""
        return f"  [{ts}] {who}{body[:400]}"

    context_body = "\n".join(_render(m) for m in messages[-_THREAD_CONTEXT_LIMIT:])
    new_text = (obs.get("text") or "").strip()

    wrapped = (
        f"## Context (last {min(len(messages), _THREAD_CONTEXT_LIMIT)} messages in this thread — already processed, grounding only)\n"
        f"{context_body}\n\n"
        f"## New this cycle\n"
        f"{new_text}"
    )

    out = dict(obs)
    out["text"] = wrapped
    return out


def _render_line(obs: dict, marker: str) -> str:
    ts = obs.get("timestamp", "")
    source = obs.get("source", "?")
    sender = obs.get("sender", "?")
    text = obs.get("text", "") or ""
    return f"{marker} [{ts}] [{source}] {sender}: {text}"


def format_signals(signals: Iterable[dict]) -> str:
    """Render a batch of observation dicts as a chronological timeline.

    Each line carries its tier as a ``[T1]`` / ``[T2]`` / ``[T3]``
    prefix. Order is by timestamp (ascending); ties fall back to
    caller order. iMessage / WhatsApp signals are augmented with
    thread context (see ``_inject_thread_context``).
    """
    signals = list(signals)
    if not signals:
        return ""

    def _sort_key(obs: dict):
        return obs.get("timestamp") or ""

    ordered = sorted(signals, key=_sort_key)
    lines = []
    for obs in ordered:
        augmented = _inject_thread_context(obs)
        tier = classify_tier(augmented)
        marker = _TIER_MARKERS.get(tier, _TIER_MARKERS[3])
        lines.append(_render_line(augmented, marker))
    return "\n".join(lines)
