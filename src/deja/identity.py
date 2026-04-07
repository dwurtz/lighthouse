"""The user behind the keyboard.

Déjà is a personal agent — every prompt needs some notion of who the
user is (name, email for outbound-detection and mailer From:, phone for
message matching, a short bio for grounding). This module centralizes that
lookup so no other code needs to know the user's name.

Source of truth is a single wiki page marked ``self: true`` in its YAML
frontmatter. By convention it lives at ``people/<slug>.md``. This keeps
identity inside the wiki (Obsidian-editable, versioned alongside everything
else) rather than scattered across code and config files.

Example self-page:

    ---
    self: true
    email: jane@example.com
    phone: "+14155551234"
    preferred_name: Jane
    aliases: [Jane, JD]
    ---

    # Jane Doe

    Jane is a product manager at Acme Corp living in San Francisco with her
    partner and two kids. She's currently focused on shipping the Q2 redesign
    and preparing for parental leave in July.

If no self-page exists, ``load_user()`` returns a generic "the user"
profile so the system still boots. ``startup_check`` surfaces the missing
page as a fixable warning in log.md.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

from deja.config import WIKI_DIR

log = logging.getLogger(__name__)


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)
_H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)

# Used when no self-page exists. Keeps prompts grammatical and generic
# enough that the LLM's output still reads naturally.
_GENERIC = {
    "slug": "",
    "name": "the user",
    "first_name": "the user",
    "email": "",
    "phone": "",
    "profile_md": "(No self-page configured. Create one at Deja/people/<your-slug>.md with `self: true` in YAML frontmatter.)",
}


@dataclass(frozen=True)
class UserProfile:
    """Everything downstream prompts and code need about the user.

    Fields are always strings — empty when unknown — so prompt ``.format()``
    calls never blow up on ``None``. ``profile_md`` is the self-page body
    (minus frontmatter), intended for direct injection into analyze/chat
    prompts as biographical grounding.
    """

    slug: str
    name: str
    first_name: str
    email: str
    phone: str
    profile_md: str

    @property
    def is_generic(self) -> bool:
        """True iff no self-page was found and we're running on the fallback."""
        return self.slug == ""

    def as_prompt_fields(self) -> dict[str, str]:
        """Return the kwargs used to ``.format()`` prompt templates.

        Every prompt that touches identity expects these four keys:
        ``user_name``, ``user_first_name``, ``user_email``, ``user_profile``.
        Centralizing the mapping here means a prompt rename touches one file.
        """
        return {
            "user_name": self.name,
            "user_first_name": self.first_name,
            "user_email": self.email,
            "user_profile": self.profile_md.strip() or _GENERIC["profile_md"],
        }


def _parse_self_page(text: str) -> tuple[dict, str, str]:
    """Split a markdown page into (frontmatter_dict, h1_title, body_md).

    Returns empty dict / empty title / original text on any parse failure.
    Body is the content after both the frontmatter block and the H1 title,
    so it can be injected directly as a bio without the title repeating.
    """
    meta: dict = {}
    body = text
    m = _FRONTMATTER_RE.match(text)
    if m:
        try:
            parsed = yaml.safe_load(m.group(1)) or {}
            if isinstance(parsed, dict):
                meta = parsed
        except Exception:
            log.debug("self-page frontmatter parse failed", exc_info=True)
        body = m.group(2)

    title = ""
    t = _H1_RE.search(body)
    if t:
        title = t.group(1).strip()
        # Drop the title line from body so it doesn't duplicate when injected
        body = body[:t.start()] + body[t.end():]

    return meta, title, body.strip()


def _find_self_slug() -> str | None:
    """Look for an explicit ``user_slug`` in config, or scan ``people/`` for
    the first page with ``self: true`` in frontmatter. Returns the slug
    (without extension), or None if nothing matches."""
    # Prefer explicit config pointer if present (cheap, deterministic)
    try:
        from deja import config as _cfg
        configured = getattr(_cfg, "USER_SLUG", "") or ""
        if configured:
            return configured
    except Exception:
        pass

    people_dir = WIKI_DIR / "people"
    if not people_dir.is_dir():
        return None
    try:
        for path in sorted(people_dir.glob("*.md")):
            try:
                head = path.read_text(encoding="utf-8", errors="replace")[:2048]
            except OSError:
                continue
            m = _FRONTMATTER_RE.match(head)
            if not m:
                continue
            try:
                meta = yaml.safe_load(m.group(1)) or {}
            except Exception:
                continue
            if isinstance(meta, dict) and meta.get("self") is True:
                return path.stem
    except OSError:
        log.debug("people/ scan failed while finding self-page", exc_info=True)
    return None


def _first_name(full: str, preferred: str) -> str:
    """Pick the informal reference form: explicit ``preferred_name`` wins,
    otherwise the first whitespace-delimited token of the full name."""
    if preferred:
        return preferred.strip()
    if not full:
        return ""
    return full.strip().split()[0]


def load_user() -> UserProfile:
    """Load the user profile from the wiki self-page.

    Returns a generic "the user" profile if no self-page exists, so the
    system still runs. Callers that need a real identity (e.g. the outbound
    email sender) should check ``profile.email`` and degrade gracefully.
    """
    slug = _find_self_slug()
    if slug is None:
        log.info("No self-page found in wiki; running with generic 'the user' identity")
        return UserProfile(
            slug="",
            name=_GENERIC["name"],
            first_name=_GENERIC["first_name"],
            email="",
            phone="",
            profile_md=_GENERIC["profile_md"],
        )

    path = WIKI_DIR / "people" / f"{slug}.md"
    if not path.exists():
        log.warning("user_slug=%s configured but %s does not exist", slug, path)
        return UserProfile(
            slug=slug,
            name=slug.replace("-", " ").title(),
            first_name=slug.split("-")[0].title(),
            email="",
            phone="",
            profile_md=_GENERIC["profile_md"],
        )

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        log.warning("failed to read self-page %s: %s", path, e)
        return UserProfile(
            slug=slug,
            name=slug.replace("-", " ").title(),
            first_name=slug.split("-")[0].title(),
            email="",
            phone="",
            profile_md=_GENERIC["profile_md"],
        )

    meta, title, body = _parse_self_page(text)
    name = title or str(meta.get("name", "")).strip() or slug.replace("-", " ").title()
    preferred = str(meta.get("preferred_name", "")).strip()

    # Contact fields are list-valued after the enrichment refactor. Prefer
    # the plural form; fall back to legacy singular for backwards compat.
    def _first_of(plural_key: str, singular_key: str) -> str:
        raw = meta.get(plural_key)
        if isinstance(raw, list) and raw:
            first = str(raw[0]).strip()
            if first:
                return first
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
        legacy = meta.get(singular_key)
        if isinstance(legacy, str) and legacy.strip():
            return legacy.strip()
        return ""

    email = _first_of("emails", "email")
    phone = _first_of("phones", "phone")

    return UserProfile(
        slug=slug,
        name=name,
        first_name=_first_name(name, preferred),
        email=email,
        phone=phone,
        profile_md=body,
    )
