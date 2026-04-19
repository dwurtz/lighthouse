"""Deterministic signal triage.

Replaces the former Groq-8B prefilter call (see ``deja.llm.prefilter``
for historical context). A cheap LLM classifier was overkill: the
actual decisions are boring (drop CI emails, drop password-reset blasts,
keep anything the user wrote) and don't need a model's judgment.

Rules, in priority order:

1. **Drop empties.** A signal with no text carries zero information.
2. **Always keep Tier 1.** User voice — outbound message, typed
   content, inner-circle sender — is never dropped, full stop.
   False negatives here are the most expensive mistake the system
   can make.
3. **Always keep Tier 2.** The user deliberately engaged that view;
   the dwell filter already paid for that decision upstream.
4. **Filter Tier 3 against a noise blocklist.** Known-automation
   senders (CI, password resets, calendar-sub blasts, marketing
   footers) get dropped. Everything else in Tier 3 is kept if it
   mentions a slug from the wiki catalog, else dropped — the
   catalog lookup is the signal-to-noise gate.

Recall-biased by design: when in doubt, keep. Token cost is cheap;
silently losing a signal that should have reached integrate is not.
"""

from __future__ import annotations

import logging
import re

from deja.signals.tiering import classify_tier

log = logging.getLogger(__name__)


# Canonical noise markers — case-insensitive substrings in sender OR
# the first 200 chars of text. Kept short: each entry must be
# unambiguously automation / bulk. Personal mail from a human will
# never contain these.
_NOISE_SENDER_PATTERNS = (
    "no-reply",
    "noreply",
    "no_reply",
    "donotreply",
    "do-not-reply",
    "notifications@github",
    "mailer-daemon",
    "postmaster@",
    "bounces+",
    "notification@",
    "notifications@",
    "alerts@",
    "updates@",
    "newsletter@",
    "marketing@",
    "hello@",
    "team@",
    "support@",
    "billing@",
    "receipts@",
)

_NOISE_TEXT_PATTERNS = (
    "unsubscribe",
    "password reset",
    "verify your email",
    "verification code",
    "two-factor",
    "confirm your email",
    "build failed",
    "build succeeded",
    "ci pipeline",
    "workflow run",
    "deploy finished",
    "calendar subscription",
    "your receipt",
    "order confirmation",
    "shipped",
)


def _is_noise(obs: dict) -> bool:
    """Return True if a Tier-3 observation looks like pure automation."""
    sender = (obs.get("sender") or "").lower()
    for pat in _NOISE_SENDER_PATTERNS:
        if pat in sender:
            return True
    text = (obs.get("text") or "").lower()[:400]
    for pat in _NOISE_TEXT_PATTERNS:
        if pat in text:
            return True
    return False


def _catalog_slugs() -> set[str]:
    """Return every slug referenced in the current wiki index.

    The index is line-based: ``- [[slug]] — summary``. We grep the
    wikilink out rather than parsing the markdown, because the index
    format is stable and this runs per cycle — a full parse would be
    wasted effort.
    """
    slugs: set[str] = set()
    try:
        from deja.wiki_catalog import render_index_for_prompt

        text = render_index_for_prompt(rebuild=False) or ""
    except Exception:
        log.debug("catalog render failed during triage", exc_info=True)
        return slugs
    for m in re.finditer(r"\[\[([^\]]+)\]\]", text):
        slug = m.group(1).strip().lower()
        if slug:
            slugs.add(slug)
    return slugs


def _mentions_catalog(obs: dict, slugs: set[str]) -> bool:
    """Does this observation's sender or text mention a known wiki slug?"""
    if not slugs:
        # With no catalog yet (fresh install), keep Tier-3 signals by
        # default — there's nothing to match against, and dropping
        # everything would leave the cycle empty.
        return True
    haystack = ((obs.get("sender") or "") + " " + (obs.get("text") or "")).lower()
    if not haystack.strip():
        return False
    # Compare against slug text AND against the hyphen-free form so
    # "jane-doe" matches "jane doe".
    for slug in slugs:
        if slug in haystack:
            return True
        loose = slug.replace("-", " ")
        if loose and loose in haystack:
            return True
    return False


def triage_signals(signal_items: list[dict]) -> list[dict]:
    """Drop noisy ambient signals; keep everything else.

    Deterministic replacement for the former LLM-based prefilter. See
    module docstring for the rule set.

    Input order is preserved on output so downstream formatting and
    audit-id threading stay stable.
    """
    if not signal_items:
        return []

    slugs = _catalog_slugs()
    kept: list[dict] = []
    for obs in signal_items:
        text = obs.get("text") or ""
        if not text.strip():
            continue
        tier = classify_tier(obs)
        if tier in (1, 2):
            kept.append(obs)
            continue
        # Tier 3: drop automation / bulk noise, then gate on catalog.
        if _is_noise(obs):
            log.info(
                "triage dropped noise [%s] %s",
                obs.get("source", "?"),
                (obs.get("sender") or "?")[:60],
            )
            continue
        if _mentions_catalog(obs, slugs):
            kept.append(obs)
        else:
            log.info(
                "triage dropped off-catalog ambient [%s] %s",
                obs.get("source", "?"),
                (obs.get("sender") or "?")[:60],
            )
    return kept
