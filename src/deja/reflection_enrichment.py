"""Contact enrichment helpers for the reflection pass.

Scans project pages for person-like names that lack their own people/
page, looks them up in macOS Contacts and recent email signals, and
formats the candidates for injection into the reflect prompt.

Extracted from reflection.py to keep the main pipeline focused.
"""

from __future__ import annotations

import json
import logging
import re

from deja.config import OBSERVATIONS_LOG, WIKI_DIR

log = logging.getLogger(__name__)

_NAME_RE = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2})\b")
_EMAIL_RE = re.compile(r"<([^>]+@[^>\s]+)>|([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})")


def find_orphan_people_with_contacts() -> list[dict]:
    """Scan project pages for person-like names that lack their own people/ page,
    then look up each in macOS Contacts and recent email signals. Returns a list
    of candidates: [{name, slug, phones, emails, mentioned_in, context}].

    The reflection LLM decides which of these warrant a new people/ page based on
    substance of the mention and quality of the contact info found. Candidates
    with no contact info are still included (the model can choose to skip them).
    """
    if not (WIKI_DIR / "projects").exists():
        return []

    existing_people = {p.stem for p in (WIKI_DIR / "people").glob("*.md")}
    existing_projects = {p.stem for p in (WIKI_DIR / "projects").glob("*.md")}

    # Gather person-like name mentions across project pages
    mentions: dict[str, list[str]] = {}
    for proj_path in (WIKI_DIR / "projects").glob("*.md"):
        text = proj_path.read_text()
        # Strip [[wiki links]] — those are already handled as entity references
        clean = re.sub(r"\[\[[^\]]+\]\]", "", text)
        for m in _NAME_RE.finditer(clean):
            name = m.group(1)
            mentions.setdefault(name, []).append(proj_path.stem)

    def _slugify(s: str) -> str:
        return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "unnamed"

    # Filter: name can't already have a people or project page, and must be
    # plausibly a person (filter out obvious non-names).
    NON_PEOPLE = {
        "blade rose", "highland cow", "peter rabbit", "superman saves",
        "new york", "los angeles", "palo alto", "san francisco", "united states",
        "google onboarding", "mayo clinic", "executive health",
    }
    orphans: dict[str, list[str]] = {}
    for name, projs in mentions.items():
        slug = _slugify(name)
        if slug in existing_people or slug in existing_projects:
            continue
        if name.lower() in NON_PEOPLE:
            continue
        orphans[name] = projs

    if not orphans:
        return []

    # Look up macOS Contacts (cached in-memory index)
    from deja.observations import contacts as contacts_mod
    if contacts_mod._name_set is None:
        contacts_mod._build_index()
    name_set = contacts_mod._name_set or set()
    phone_index = contacts_mod._phone_index or {}

    # Scan last ~90 days of signal_log for email mentions of each orphan
    email_hits: dict[str, set[str]] = {}
    context_hits: dict[str, str] = {}
    if OBSERVATIONS_LOG.exists():
        for line in OBSERVATIONS_LOG.read_text().splitlines()[-5000:]:
            line = line.strip()
            if not line:
                continue
            try:
                sig = json.loads(line)
            except json.JSONDecodeError:
                continue
            if sig.get("source") != "email":
                continue
            sender = sig.get("sender", "") or ""
            body = sig.get("text", "") or ""
            blob = f"{sender}\n{body}"
            for name in orphans:
                if name.lower() in blob.lower():
                    # Extract emails from sender/body
                    for m in _EMAIL_RE.finditer(blob):
                        addr = m.group(1) or m.group(2)
                        if addr:
                            email_hits.setdefault(name, set()).add(addr)
                    # Capture a short context snippet on first hit
                    if name not in context_hits:
                        context_hits[name] = body[:160]

    candidates = []
    for name, projs in sorted(orphans.items()):
        phones = [p for p, n in phone_index.items() if n.lower() == name.lower()][:3]
        emails = sorted(email_hits.get(name, set()))[:3]
        in_contacts = name.lower() in name_set
        if not (phones or emails or in_contacts):
            # Skip names we know nothing about — the model can't do anything useful
            continue
        candidates.append({
            "name": name,
            "slug": _slugify(name),
            "phones": phones,
            "emails": emails,
            "in_macos_contacts": in_contacts,
            "mentioned_in": sorted(set(projs))[:5],
            "context_snippet": context_hits.get(name, ""),
        })

    log.info("Reflection enrichment: found %d orphan people candidates with contact info",
             len(candidates))
    return candidates


def format_orphan_candidates(candidates: list[dict]) -> str:
    """Format orphan candidates for injection into the reflect prompt."""
    if not candidates:
        return "(none — every person mentioned in projects already has a page or no contact info was found)"
    lines = []
    for c in candidates:
        bits = [f"**{c['name']}** (slug: `{c['slug']}`)"]
        if c["phones"]:
            bits.append(f"phones: {', '.join(c['phones'])}")
        if c["emails"]:
            bits.append(f"emails: {', '.join(c['emails'])}")
        if c["in_macos_contacts"]:
            bits.append("in macOS Contacts")
        bits.append(f"mentioned in: {', '.join(c['mentioned_in'])}")
        if c["context_snippet"]:
            bits.append(f"context: {c['context_snippet']}")
        lines.append("- " + " · ".join(bits))
    return "\n".join(lines)
