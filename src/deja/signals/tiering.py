"""Classify an observation into one of three priority tiers.

The integrate prompt treats observations very differently depending on
whether the user authored them, engaged them deliberately, or merely
happened to have them cross the screen. Rather than spread that
heuristic across the prompt and the formatter, we compute a single
``tier`` (1/2/3) per signal up front — a pure function of the
observation dict plus two cached lookups (user emails, inner-circle
slugs).

``classify_tier`` is the only public entry point; the helpers are
private. The two caches (``_user_emails``, ``_inner_circle_slugs``)
are module-level and lazy: first call populates them, subsequent calls
reuse. ``reset_caches()`` is exposed for tests.

Why caches and not recompute-every-time: the classifier runs once per
signal per cycle — dozens of calls — and both lookups touch the
filesystem (self-page frontmatter + every people/*.md frontmatter).
The caches are read-only snapshots; if the user edits their wiki, the
next process restart picks up the change. That's a deliberate trade —
it means the user has to restart Deja to promote someone to inner
circle, but it keeps classification cheap and deterministic.
"""

from __future__ import annotations

import logging
import re
from typing import Iterable

import yaml

from deja.config import WIKI_DIR

log = logging.getLogger(__name__)


# Sources whose TEXT is authored by the user regardless of sender string.
# Voice dictation and typed content never come from anyone else; treat
# them as Tier 1 by source alone.
_USER_AUTHORED_SOURCES = {"microphone", "voice", "typed", "chat"}

# Sources where the "sender" string literally being "You" means outbound.
# (Matches the convention in observations/imessage.py + whatsapp.py.)
_SELF_SENDER_SOURCES = {"imessage", "whatsapp"}

# Window-title substrings that indicate an inbox or list view — the
# opposite of a focused content view. If a screenshot's Focused header
# matches one of these, it's Tier 3 (ambient), not Tier 2.
_INBOX_TITLE_HINTS = (
    "inbox",
    "all mail",
    "notifications",
    "activity",
    "updates",
    "home",
    "feed",
)

_FOCUSED_HEADER_RE = re.compile(
    r"\[Focused:\s*[^—\-\]]+[—\-]\s*\"([^\"]+)\"\s*\]",
    re.IGNORECASE,
)

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)


# Module-level caches — populated lazily on first use.
_user_emails: frozenset[str] | None = None
_inner_circle_slugs: frozenset[str] | None = None
# Inner-circle contact identifiers: E.164 phone numbers and lowercased
# emails from people/*.md frontmatter for users with inner_circle: true.
# These are the ONLY match keys — name matching is out of scope
# (contact resolution is solved at the OS layer, not by fuzzy name rules).
_inner_circle_phones: frozenset[str] | None = None
_inner_circle_emails: frozenset[str] | None = None


def reset_caches() -> None:
    """Drop cached user emails + inner-circle slugs. Test helper."""
    global _user_emails, _inner_circle_slugs
    global _inner_circle_phones, _inner_circle_emails
    _user_emails = None
    _inner_circle_slugs = None
    _inner_circle_phones = None
    _inner_circle_emails = None


def _normalize_phone(raw: str) -> str:
    """Return E.164-like ``+<digits>`` for any phone-ish string.

    - Strips all non-digit/plus characters.
    - If 10 digits and no leading +, assume US: ``+1XXXXXXXXXX``.
    - If 11+ digits and no +, prepend +.
    - Empty string for inputs with no digits.

    Used for BOTH the frontmatter side (``phones:`` list) and the
    sender side, so ``(516) 987-9840`` on a page matches ``+15169879840``
    in an iMessage sender string.
    """
    if not raw:
        return ""
    digits = "".join(c for c in raw if c.isdigit())
    if not digits:
        return ""
    if raw.strip().startswith("+"):
        return "+" + digits
    if len(digits) == 10:
        return "+1" + digits
    return "+" + digits


_PHONE_LIKE_RE = re.compile(r"(\+?\d[\d\-\.\s\(\)]{6,}\d)")
_EMAIL_LIKE_RE = re.compile(r"([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})")


def _load_user_emails() -> frozenset[str]:
    """Return every email address the user claims on their self-page.

    Union of ``deja.identity.load_user().email`` (the primary) and the
    full ``emails:`` frontmatter list (personal + work + any alias).
    Lowercased so outbound detection is case-insensitive. Empty set
    when no self-page exists — the classifier falls back to treating
    every email as inbound in that case, which is the safe default.
    """
    emails: set[str] = set()
    try:
        from deja.identity import load_user

        primary = (load_user().email or "").strip().lower()
        if primary:
            emails.add(primary)
    except Exception:
        log.debug("load_user failed while building email cache", exc_info=True)

    # Also read the full `emails:` list directly — load_user() only
    # exposes the first entry as ``.email``.
    people_dir = WIKI_DIR / "people"
    if people_dir.is_dir():
        try:
            for path in people_dir.glob("*.md"):
                meta = _read_frontmatter(path)
                if not meta or meta.get("self") is not True:
                    continue
                raw = meta.get("emails")
                if isinstance(raw, list):
                    for e in raw:
                        if isinstance(e, str) and e.strip():
                            emails.add(e.strip().lower())
                elif isinstance(raw, str) and raw.strip():
                    emails.add(raw.strip().lower())
                legacy = meta.get("email")
                if isinstance(legacy, str) and legacy.strip():
                    emails.add(legacy.strip().lower())
                break  # one self-page max
        except OSError:
            log.debug("people/ scan failed while loading user emails", exc_info=True)

    return frozenset(emails)


def _read_frontmatter(path) -> dict:
    """Return the YAML frontmatter dict from a markdown file, or empty."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")[:4096]
    except OSError:
        return {}
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}
    try:
        parsed = yaml.safe_load(m.group(1)) or {}
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        log.debug("frontmatter parse failed for %s", path, exc_info=True)
    return {}


def _load_inner_circle() -> tuple[frozenset[str], frozenset[str], frozenset[str]]:
    """Scan people/*.md for ``inner_circle: true`` and return three sets:

    1. ``slugs``  — canonical page stems.
    2. ``phones`` — every ``phones:`` entry, normalized to E.164.
    3. ``emails`` — every ``emails:`` entry, lowercased.

    These are the ONLY match keys. Name / alias matching is deliberately
    out of scope — contact resolution is the OS's job; we just check
    the already-structured phone/email fields that iMessage / WhatsApp /
    Gmail give us in the sender string.
    """
    slugs: set[str] = set()
    phones: set[str] = set()
    emails: set[str] = set()
    people_dir = WIKI_DIR / "people"
    if not people_dir.is_dir():
        return frozenset(slugs), frozenset(phones), frozenset(emails)

    try:
        for path in sorted(people_dir.glob("*.md")):
            meta = _read_frontmatter(path)
            if meta.get("inner_circle") is not True:
                continue
            slugs.add(path.stem)
            raw_phones = meta.get("phones")
            if isinstance(raw_phones, list):
                for p in raw_phones:
                    n = _normalize_phone(str(p) if p is not None else "")
                    if n:
                        phones.add(n)
            elif isinstance(raw_phones, str):
                n = _normalize_phone(raw_phones)
                if n:
                    phones.add(n)
            raw_emails = meta.get("emails")
            if isinstance(raw_emails, list):
                for e in raw_emails:
                    if isinstance(e, str) and "@" in e:
                        emails.add(e.strip().lower())
            elif isinstance(raw_emails, str) and "@" in raw_emails:
                emails.add(raw_emails.strip().lower())
    except OSError:
        log.debug("people/ scan failed loading inner-circle", exc_info=True)

    return frozenset(slugs), frozenset(phones), frozenset(emails)


def load_inner_circle_slugs() -> frozenset[str]:
    """Return slugs of every ``people/*.md`` with ``inner_circle: true``."""
    global _inner_circle_slugs, _inner_circle_phones, _inner_circle_emails
    if _inner_circle_slugs is None:
        _inner_circle_slugs, _inner_circle_phones, _inner_circle_emails = _load_inner_circle()
    return _inner_circle_slugs


def _inner_circle_phones_set() -> frozenset[str]:
    global _inner_circle_phones
    if _inner_circle_phones is None:
        load_inner_circle_slugs()
    return _inner_circle_phones or frozenset()


def _inner_circle_emails_set() -> frozenset[str]:
    global _inner_circle_emails
    if _inner_circle_emails is None:
        load_inner_circle_slugs()
    return _inner_circle_emails or frozenset()


def _sender_matches_inner_circle(sender: str) -> bool:
    """True iff the sender string contains a phone number or email
    address matching any inner-circle person's ``phones:`` / ``emails:``
    frontmatter. Phones are compared after E.164 normalization on both
    sides."""
    if not sender:
        return False
    phones = _inner_circle_phones_set()
    emails = _inner_circle_emails_set()
    if not phones and not emails:
        return False
    low = sender.lower()
    for em in _EMAIL_LIKE_RE.findall(sender):
        if em.lower() in emails:
            return True
    for raw in _PHONE_LIKE_RE.findall(sender):
        n = _normalize_phone(raw)
        if n and n in phones:
            return True
    return False


def _get_user_emails() -> frozenset[str]:
    global _user_emails
    if _user_emails is None:
        _user_emails = _load_user_emails()
    return _user_emails


def _sender_email_is_user(sender: str, user_emails: Iterable[str]) -> bool:
    """True iff ``sender`` looks like an outbound email authored by the user.

    Email observations set sender to ``"<User Name> → recipient"`` or
    ``"user@host → recipient"``. We look on the left of the arrow for
    any address in the user's known-emails set.
    """
    if not sender:
        return False
    left = sender.split("→", 1)[0].lower()
    for email in user_emails:
        if email and email in left:
            return True
    return False


def _sender_slug(sender: str) -> str:
    """Best-effort slugification of a sender string for inner-circle lookup.

    The wiki slug convention is ``lowercase-hyphens``. Senders arrive as
    ``"Firstname Lastname"`` (iMessage/WhatsApp), ``"Name <email>"`` or
    just an email. We strip any ``<...>`` block, drop punctuation, and
    hyphenate — good enough for direct-match against people/*.md stems.
    """
    if not sender:
        return ""
    # Drop any "<email>" suffix common in email From: strings.
    name = re.sub(r"<[^>]+>", "", sender).strip()
    # Drop a trailing email if sender is just "foo@bar.com"
    if "@" in name and " " not in name:
        name = name.split("@", 1)[0]
    name = re.sub(r"[^A-Za-z0-9\s]", " ", name)
    parts = [p for p in name.lower().split() if p]
    return "-".join(parts)


def _is_focused_attention_screenshot(text: str) -> bool:
    """Tier-2 heuristic for screenshots.

    A screenshot is "attention" — the user deliberately engaged a
    single content view — when its text contains a
    ``[Focused: <app> — "<window title>"]`` header AND the title is
    not an inbox / list / notifications view.

    We're intentionally conservative: if no Focused header is present
    the signal falls through to Tier 3. Upstream dwell-based dedup
    already filters transient frames, so this is the final gate.
    """
    if not text:
        return False
    m = _FOCUSED_HEADER_RE.search(text)
    if not m:
        return False
    title = m.group(1).strip().lower()
    if not title:
        return False
    for hint in _INBOX_TITLE_HINTS:
        if hint in title:
            return False
    return True


def classify_tier(obs: dict) -> int:
    """Return the priority tier (1, 2, or 3) for one observation dict.

    Pure function modulo module-level caches (user emails, inner-circle
    slugs). See module docstring for tier definitions.
    """
    source = (obs.get("source") or "").lower()
    sender = obs.get("sender") or ""
    text = obs.get("text") or ""

    # ---- Tier 1: user-authored or inner-circle authored ----
    if source in _USER_AUTHORED_SOURCES:
        return 1

    if source in _SELF_SENDER_SOURCES and sender == "You":
        return 1

    if source == "email":
        # [SENT] prefix is produced only by the email collector for
        # outbound messages. Trust it unconditionally — it's the
        # authoritative marker, independent of whether the sender
        # string contains a recognizable email address.
        if text.startswith("[SENT]"):
            return 1
        # [ENGAGED] prefix means: an incoming reply on a thread the user
        # has already responded to. Engagement is a stronger signal than
        # who's in the inner circle — if David hit Reply once, every
        # subsequent message in that thread matters.
        if text.startswith("[ENGAGED]"):
            return 1
        if "→" in sender:
            user_emails = _get_user_emails()
            if not user_emails or _sender_email_is_user(sender, user_emails):
                return 1

    if source == "screenshot" and text.startswith("[SENT]"):
        return 1

    # Inner-circle inbound: messages FROM a person flagged inner_circle.
    # Outbound messages were already caught above (sender == "You").
    # Match against aliases/first-name/preferred-name not just slug,
    # because iMessage/WhatsApp senders arrive as the contact's display
    # name (e.g. "Nie (+15169879840)") — never the wiki slug.
    if source in ("imessage", "whatsapp", "email"):
        if _sender_matches_inner_circle(sender):
            return 1

    # ---- Tier 2: deliberate screen engagement ----
    if source == "screenshot" and _is_focused_attention_screenshot(text):
        return 2

    # ---- Tier 3: everything else ----
    return 3
