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


def _is_threaded_source(obs: dict) -> bool:
    """True when ``obs`` comes from a source we apply thread-context
    injection to (iMessage / WhatsApp)."""
    return (obs.get("source") or "").lower() in _THREADED_SOURCES


def _thread_identifiers(obs: dict) -> set[str]:
    """Collect every string that identifies this observation's thread.

    We return a set so matching against historical rows works even when
    the two sides of the comparison stored the identifier under
    different shapes — new per-turn rows carry ``chat_id``, legacy
    digests only have ``sender``, and during the Swift migration window
    both may coexist in ``observations.jsonl``. Using a set means a row
    matches if ANY of its identifiers overlap ANY of the current
    observation's — no asymmetry between "current has chat_id, history
    has sender" and the reverse.
    """
    idents: set[str] = set()
    for field in ("chat_id", "chat_label", "sender"):
        v = obs.get(field)
        if isinstance(v, str) and v.strip():
            idents.add(v.strip())
    return idents


def _load_thread_context(
    obs: dict,
    exclude_id: str,
    limit: int,
) -> list[dict]:
    """Return up to ``limit`` prior messages in the same thread as
    ``obs``, in chronological order (oldest first).

    Scans ``observations.jsonl`` from the end backward — file is
    append-only so recent messages are at the tail. Stops early once
    we've collected ``limit`` matches. Excludes any observation whose
    ``id_key`` matches ``exclude_id`` (usually the current signal
    itself).

    Thread matching is shape-agnostic: we collect every identifier
    field (``chat_id`` / ``chat_label`` / ``sender``) from BOTH the
    current observation and each historical row, and accept the match
    if the two sets overlap. This keeps the Swift-migration window
    clean — new-shape current + legacy-shape history still threads,
    and vice versa.
    """
    if not OBSERVATIONS_LOG.exists():
        return []

    source = (obs.get("source") or "").lower()
    want_idents = _thread_identifiers(obs)
    if not want_idents:
        return []

    try:
        with OBSERVATIONS_LOG.open(encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        log.debug("could not read %s for thread context", OBSERVATIONS_LOG, exc_info=True)
        return []

    hits: list[dict] = []
    for line in reversed(lines):
        if not line.strip():
            continue
        # Cheap substring pre-filter: skip lines where NONE of our
        # identifiers appear in the raw bytes. Avoids JSON-parsing the
        # vast majority of lines that can't match.
        if not any(ident in line for ident in want_idents):
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        if (d.get("source") or "").lower() != source:
            continue
        if not (want_idents & _thread_identifiers(d)):
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
    """Return a list of ``(speaker, body)`` pairs from one observation.

    Three shapes are supported:

    1. **Per-turn (current)** — the observation carries a ``speaker``
       field. One pair: ``(speaker, text)``.
    2. **CONVERSATION digest (legacy)** — text starts with
       ``CONVERSATION with <chat> (N messages, ...):`` and is followed
       by indented ``  <sender>: <body>`` lines. We peel the digest.
       This path exists for the ~20MB of historical observations.jsonl
       entries that pre-date the per-turn migration.
    3. **Single-message legacy** — neither of the above. We return a
       single pair with empty speaker, letting the downstream dedup
       match on body only.
    """
    text = (obs.get("text") or "").strip()
    if not text:
        return []

    # Shape 1: per-turn row with explicit speaker.
    speaker = obs.get("speaker")
    if speaker:
        return [(speaker, text)]

    # Shape 2: legacy CONVERSATION digest.
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

    # Shape 3: single-message legacy (no speaker, no digest header).
    return [("", text)]


def _inject_thread_context(obs: dict) -> dict:
    """Return a copy of ``obs`` with ``## Context`` / ``## New``
    sections prepended to its text when it's a threaded source.

    If the source isn't threaded, or no prior messages exist, returns
    the observation unchanged.
    """
    if not _is_threaded_source(obs):
        return obs

    prior_obs = _load_thread_context(
        obs, exclude_id=obs.get("id_key") or "", limit=_THREAD_CONTEXT_LIMIT
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
    text = obs.get("text", "") or ""

    # For threaded messaging: render "<chat_label> / <speaker>" so both
    # "who's in the room" and "who actually said this" are in the line.
    # Falls back to plain sender for every other source and for legacy
    # observations missing speaker.
    speaker = obs.get("speaker") or ""
    chat_label = obs.get("chat_label") or ""
    sender = obs.get("sender", "?")
    if source in _THREADED_SOURCES and speaker:
        label = chat_label or sender
        if label and label != speaker:
            who = f"{label} / {speaker}"
        else:
            who = speaker
        return f"{marker} [{ts}] [{source}] {who}: {text}"
    return f"{marker} [{ts}] [{source}] {sender}: {text}"


def _with_raw_ocr(obs: dict) -> dict:
    """Return a copy of obs with screenshot.text replaced by the raw OCR sidecar.

    Used by the Claude shadow experiment: feed the integrator the
    pixel-to-text output BEFORE the preprocess VLM had a chance to
    synthesize (and hallucinate) structured extractions. Non-screenshot
    signals pass through unchanged. Missing sidecar → pass through
    (preprocessed text is all we've got).
    """
    if obs.get("source") != "screenshot":
        return obs
    id_key = obs.get("id_key") or ""
    if not id_key:
        return obs
    try:
        from deja.raw_ocr_sidecar import read as _read_sidecar
        raw = _read_sidecar(id_key)
    except Exception:
        raw = None
    if not raw:
        return obs
    out = dict(obs)
    # Preserve the bracketed app/window header the formatter adds so
    # tier classification / focused-attention detection still work.
    # The preprocessed text shape is "[Label]\n\n<body>"; swap only
    # the body.
    preprocessed = obs.get("text") or ""
    header_end = preprocessed.find("\n\n")
    if header_end != -1:
        out["text"] = preprocessed[: header_end + 2] + raw
    else:
        out["text"] = raw
    return out


def format_signals(signals: Iterable[dict], *, use_raw_ocr: bool = False) -> str:
    """Render a batch of observation dicts as a chronological timeline.

    Each line carries its tier as a ``[T1]`` / ``[T2]`` / ``[T3]``
    prefix. Order is by timestamp (ascending); ties fall back to
    caller order. iMessage / WhatsApp signals are augmented with
    thread context (see ``_inject_thread_context``).

    When ``use_raw_ocr`` is True, screenshot observations have their
    text replaced with the raw Apple Vision OCR sidecar (written by
    the agent pipeline before preprocess runs). Used by the Claude
    integrate shadow to compare reasoning quality on unadulterated
    input vs. preprocessed. Production stays ``False``.
    """
    signals = list(signals)
    if not signals:
        return ""

    def _sort_key(obs: dict):
        return obs.get("timestamp") or ""

    ordered = sorted(signals, key=_sort_key)
    lines = []
    for obs in ordered:
        if use_raw_ocr:
            obs = _with_raw_ocr(obs)
        augmented = _inject_thread_context(obs)
        tier = classify_tier(augmented)
        marker = _TIER_MARKERS.get(tier, _TIER_MARKERS[3])
        lines.append(_render_line(augmented, marker))
    return "\n".join(lines)
