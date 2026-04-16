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

No per-signal truncation — Flash has a 1M token context window and
the signal text is whatever the collector captured.
"""

from __future__ import annotations

from deja.signals.tiering import classify_tier


_TIER_MARKERS = {1: "[T1]", 2: "[T2]", 3: "[T3]"}


def _render_line(obs: dict, marker: str) -> str:
    ts = obs.get("timestamp", "")
    source = obs.get("source", "?")
    sender = obs.get("sender", "?")
    text = obs.get("text", "") or ""
    return f"{marker} [{ts}] [{source}] {sender}: {text}"


def format_signals(signals: list[dict]) -> str:
    """Render a batch of observation dicts as a chronological timeline.

    Each line carries its tier as a ``[T1]`` / ``[T2]`` / ``[T3]``
    prefix. Order is by timestamp (ascending); ties fall back to
    caller order.
    """
    if not signals:
        return ""

    def _sort_key(obs: dict):
        return obs.get("timestamp") or ""

    ordered = sorted(signals, key=_sort_key)
    lines = []
    for obs in ordered:
        tier = classify_tier(obs)
        marker = _TIER_MARKERS.get(tier, _TIER_MARKERS[3])
        lines.append(_render_line(obs, marker))
    return "\n".join(lines)


