"""Back-compat shim for the former Groq-8B signal triage.

This module used to host a batched LLM call that classified each
observation as relevant/noise before the integrate cycle spent Flash
tokens on it. That heuristic is now deterministic — see
``deja.signals.triage`` for the rule-based replacement and its
rationale.

We keep this file (and its name) only because:

1. ``analysis_cycle._run_analysis_cycle_body`` imports
   ``TRIAGE_SOURCES`` to partition outbound-vs-inbound signals during
   the cycle-level screenshot-age filter. That constant still means
   "sources that the inbound-triage rules consider" and is reused by
   the deterministic triage's caller.
2. Older tests (``tests/test_prefilter_format.py``) import helpers
   from this module directly.

No LLM calls happen here anymore. The ``triage_signals`` entry point
delegates straight to ``deja.signals.triage.triage_signals``.
"""

from __future__ import annotations

import logging

from deja.signals.triage import triage_signals as _deterministic_triage

log = logging.getLogger(__name__)


# Kept for ``analysis_cycle`` + legacy tests: the set of sources that
# the inbound-triage partition considers. Non-message sources still
# bypass triage entirely (they're low-volume and curated upstream).
TRIAGE_SOURCES = {"imessage", "whatsapp", "email", "browser"}


def _format_signals_block(items: list[dict]) -> str:
    """Render a numbered list of signals for debug / legacy test use.

    Same output shape as the old batched-triage prompt block, kept
    verbatim so tests that pinned the format still pass.
    """
    lines: list[str] = []
    for idx, d in enumerate(items, start=1):
        source = d.get("source", "?")
        sender = (d.get("sender") or "?").replace("\n", " ")[:80]
        text = (d.get("text") or "").replace("\n", " ").strip()[:600]
        lines.append(f"{idx}. [{source}] {sender}: {text}")
    return "\n".join(lines)


def _load_index_md() -> str:
    """Read the current wiki index (no rebuild). Empty string on miss.

    The deterministic triage reads the catalog directly; this helper
    survives for legacy tests and any ad-hoc callers.
    """
    try:
        from deja.wiki_catalog import render_index_for_prompt

        return render_index_for_prompt(rebuild=False) or ""
    except Exception:
        log.debug("_load_index_md failed", exc_info=True)
        return ""


def load_index_md() -> str:
    """Back-compat alias for callers that still import the old name."""
    return _load_index_md()


def triage_signals(signal_items: list[dict]) -> list[dict]:
    """Deterministic drop-in for the old async Groq-8B triage.

    Kept sync + named the same so the analysis cycle's import path
    stays stable across the migration. All the real logic lives in
    ``deja.signals.triage``.
    """
    return _deterministic_triage(signal_items)
