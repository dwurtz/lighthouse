"""Tier-aware signal formatter for the integrate prompt.

The integrate LLM gets one big block of observations per cycle. The
flat formatter that used to live in ``agent/analysis_cycle.py``
rendered every observation as a peer, which meant the model had to
rediscover priority from source strings every call. The tiered
formatter here does that grouping up front: Tier 1 first, Tier 2
second, Tier 3 last, each under a plain-text header so the model can
anchor on them.

No per-signal truncation — Flash-Lite has a 1M token context window
and the signal text is whatever the collector captured.

The headers and ordering are a **contract** between this formatter
and the integrate prompt. If you rename a header here, update
``default_assets/prompts/integrate.md`` to match.
"""

from __future__ import annotations

from deja.signals.tiering import classify_tier


_TIER_HEADERS = {
    1: "## Tier 1 — Voice (user-authored or inner-circle)",
    2: "## Tier 2 — Attention (user engaged this view)",
    3: "## Tier 3 — Ambient (background context)",
}


def _render_line(obs: dict) -> str:
    ts = obs.get("timestamp", "")
    source = obs.get("source", "?")
    sender = obs.get("sender", "?")
    text = obs.get("text", "") or ""
    return f"[{ts}] [{source}] {sender}: {text}"


def format_signals(signals: list[dict]) -> str:
    """Render a batch of observation dicts grouped by tier.

    Tiers with no signals are omitted — we don't want to mislead the
    prompt with empty headers. Order within a tier is the caller's
    order (typically chronological).
    """
    if not signals:
        return ""

    buckets: dict[int, list[dict]] = {1: [], 2: [], 3: []}
    for obs in signals:
        tier = classify_tier(obs)
        if tier not in buckets:
            tier = 3
        buckets[tier].append(obs)

    sections: list[str] = []
    for tier in (1, 2, 3):
        items = buckets[tier]
        if not items:
            continue
        body = "\n".join(_render_line(o) for o in items)
        sections.append(f"{_TIER_HEADERS[tier]}\n{body}")

    return "\n\n".join(sections)
