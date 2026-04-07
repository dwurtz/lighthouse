"""Contact enrichment for wiki people pages.

Once a day (during nightly cleanup), walk every ``people/*.md`` page and try
to augment its frontmatter with real contact info pulled from the user's
actual data sources:

  1. **macOS Contacts** (AddressBook SQLite) — authoritative for people
     the user has already added to Contacts. Zero-cost local lookup.
  2. **Gmail** (via ``gws gmail``) — catches work contacts and anyone the
     user corresponds with but hasn't formally added to Contacts. Expensive
     (network + quota), so only runs for pages that still have a missing
     email after the macOS pass.
  3. *(V2, not yet implemented)* iMessage chat.db handles — recovers
     phone/email for people the user texts but who aren't in Contacts.

Merge rules:

  - **Never overwrite.** If a page already has ``email:`` in frontmatter,
    leave it alone — the user or a prior nightly pass is the source of
    truth for that field.
  - **Append new values** to list fields (``phones``, ``aliases``) if
    they're not already present. Phones are normalized to digits-only for
    dedupe comparison; the display form is whatever the source returned.
  - **Ambiguous match = skip.** If two macOS contacts both named "Justin"
    match a page titled "Justin (Molly's Dad)", don't guess — log the
    ambiguity so nightly thoughts can surface it to the user.
  - **Preserve all non-contact frontmatter fields** verbatim (``aliases``,
    ``keywords``, ``domains``, ``self``, etc.).

The enrichment report is logged to ``deja.log`` and a summary entry
is appended to ``log.md`` so David can see what changed in Obsidian.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from deja.config import WIKI_DIR

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class ContactMatch:
    """Raw contact info pulled from a single source for one page.

    All fields are lists so we can aggregate across sources before merging.
    ``sources`` tracks provenance for logging (e.g. ``["macos", "gmail"]``).
    """
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    company: str = ""
    nicknames: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    ambiguous: bool = False

    def is_empty(self) -> bool:
        return not (self.emails or self.phones or self.company or self.nicknames)


@dataclass
class PageEnrichment:
    """Per-page enrichment record for the nightly report."""
    slug: str
    added_emails: list[str] = field(default_factory=list)
    added_phones: list[str] = field(default_factory=list)
    added_company: str = ""
    sources: list[str] = field(default_factory=list)
    ambiguous: bool = False

    def brief(self) -> str:
        parts = []
        if self.added_emails:
            parts.append(f"+email {', '.join(self.added_emails)}")
        if self.added_phones:
            parts.append(f"+phone {', '.join(self.added_phones)}")
        if self.added_company:
            parts.append(f"+company {self.added_company}")
        if self.ambiguous:
            parts.append("(ambiguous macos match — skipped)")
        return f"{self.slug}: {'; '.join(parts) if parts else 'no change'}"


@dataclass
class EnrichmentReport:
    pages_scanned: int = 0
    pages_changed: int = 0
    changes: list[PageEnrichment] = field(default_factory=list)
    ambiguous: list[str] = field(default_factory=list)

    def brief(self) -> str:
        if self.pages_changed == 0 and not self.ambiguous:
            return f"enrich: {self.pages_scanned} pages scanned, no changes"
        parts = []
        if self.pages_changed:
            parts.append(f"enriched {self.pages_changed} page(s)")
        if self.ambiguous:
            parts.append(f"{len(self.ambiguous)} ambiguous match(es)")
        return f"enrich: {', '.join(parts)}"


# ---------------------------------------------------------------------------
# Source 1 — macOS Contacts (rich query)
# ---------------------------------------------------------------------------

_macos_cache: list[dict] | None = None


def _load_macos_contacts() -> list[dict]:
    """Read every contact with first/last/org + aggregated phones + emails.

    Cached for the duration of the process. Returns an empty list on any
    database access failure — enrichment is best-effort.
    """
    global _macos_cache
    if _macos_cache is not None:
        return _macos_cache

    out: list[dict] = []
    ab_dir = Path.home() / "Library" / "Application Support" / "AddressBook" / "Sources"
    if not ab_dir.exists():
        _macos_cache = out
        return out

    for db_path in ab_dir.glob("*/AddressBook-v22.abcddb"):
        try:
            conn = sqlite3.connect(str(db_path))
            rows = conn.execute("""
                SELECT
                    r.Z_PK,
                    COALESCE(r.ZFIRSTNAME, ''),
                    COALESCE(r.ZLASTNAME, ''),
                    COALESCE(r.ZORGANIZATION, ''),
                    COALESCE(r.ZNICKNAME, ''),
                    (
                        SELECT GROUP_CONCAT(DISTINCT p.ZFULLNUMBER)
                        FROM ZABCDPHONENUMBER p
                        WHERE p.ZOWNER = r.Z_PK
                    ) AS phones,
                    (
                        SELECT GROUP_CONCAT(DISTINCT e.ZADDRESS)
                        FROM ZABCDEMAILADDRESS e
                        WHERE e.ZOWNER = r.Z_PK
                    ) AS emails
                FROM ZABCDRECORD r
                WHERE r.ZFIRSTNAME IS NOT NULL OR r.ZLASTNAME IS NOT NULL OR r.ZORGANIZATION IS NOT NULL
            """).fetchall()
            conn.close()

            for (_pk, first, last, org, nick, phones, emails) in rows:
                full = f"{first} {last}".strip()
                if not full and not org:
                    continue
                out.append({
                    "full_name": full,
                    "first": first.strip(),
                    "last": last.strip(),
                    "org": (org or "").strip(),
                    "nickname": (nick or "").strip(),
                    "phones": [p.strip() for p in (phones or "").split(",") if p.strip()],
                    "emails": [e.strip() for e in (emails or "").split(",") if e.strip()],
                })
        except Exception as e:
            log.debug("macos contacts read failed for %s: %s", db_path, e)

    _macos_cache = out
    log.info("contact_enrich: loaded %d macOS contacts for enrichment", len(out))
    return out


def _name_candidates(page_name: str, aliases: list[str]) -> set[str]:
    """Produce the set of lowercase candidate forms to match against Contacts.

    Includes the page title, each alias, and the title with any parenthetical
    suffix stripped (so ``Justin (Molly's Dad)`` matches just ``Justin``,
    which usually isn't helpful on its own — intentionally kept last so that
    exact-full-name matches win).
    """
    cands: set[str] = set()
    if page_name:
        cands.add(page_name.strip().lower())
        # Strip parenthetical qualifier if present
        stripped = re.sub(r"\s*\([^)]*\)\s*", "", page_name).strip()
        if stripped and stripped.lower() != page_name.strip().lower():
            cands.add(stripped.lower())
    for a in aliases or []:
        if isinstance(a, str) and a.strip():
            cands.add(a.strip().lower())
    return cands


def lookup_macos_contact(
    page_name: str,
    aliases: list[str] | None = None,
) -> ContactMatch:
    """Look up a person in macOS Contacts by name or alias.

    Returns an empty ContactMatch if no match. Returns ``ambiguous=True``
    if multiple distinct contacts match — the caller should skip writing
    anything in that case rather than pick a guess.
    """
    contacts = _load_macos_contacts()
    if not contacts:
        return ContactMatch()

    candidates = _name_candidates(page_name, aliases or [])

    # Match order: exact full name > nickname > first name only (if unique)
    exact_matches: list[dict] = []
    for c in contacts:
        keys = {
            c["full_name"].lower(),
            c["nickname"].lower() if c["nickname"] else "",
            c["first"].lower() if c["first"] else "",
            c["org"].lower() if c["org"] else "",
        }
        keys.discard("")
        if keys & candidates:
            exact_matches.append(c)

    if not exact_matches:
        return ContactMatch()

    # Dedupe by full name + org (case where same person appears across DBs)
    unique: dict[tuple[str, str], dict] = {}
    for m in exact_matches:
        key = (m["full_name"].lower(), m["org"].lower())
        if key not in unique:
            unique[key] = m
        else:
            # Merge phones/emails across duplicate records
            prev = unique[key]
            for p in m["phones"]:
                if p not in prev["phones"]:
                    prev["phones"].append(p)
            for e in m["emails"]:
                if e not in prev["emails"]:
                    prev["emails"].append(e)

    distinct = list(unique.values())
    if len(distinct) > 1:
        # Multiple distinct people match — don't guess
        return ContactMatch(ambiguous=True, sources=["macos"])

    c = distinct[0]
    return ContactMatch(
        emails=list(c["emails"]),
        phones=list(c["phones"]),
        company=c["org"],
        nicknames=[c["nickname"]] if c["nickname"] else [],
        sources=["macos"],
    )


# ---------------------------------------------------------------------------
# Source 2 — Gmail header scrape via gws
# ---------------------------------------------------------------------------

_EMAIL_HEADER_RE = re.compile(r"<?([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})>?")
_GMAIL_TIMEOUT_SEC = 20


def lookup_gmail_for_name(name: str, *, max_results: int = 8) -> ContactMatch:
    """Query Gmail for messages mentioning a person by name, return email addresses seen.

    Gmail's ``from:"Tom Peffer"`` operator requires an exact header match
    and returns 0 for most real contacts — the full-text query ``Tom Peffer``
    (no operators, no quotes) is smarter: it matches the name in any
    indexed field, and for personal contacts it's usually narrow enough.

    For each candidate message we fetch the From/To/Cc headers, extract
    email addresses whose display-name portion contains the target name,
    and rank by frequency. The top-ranked address is the canonical one.

    Falls back to an empty result on any failure (missing ``gws``, network
    error, timeout, no matches, name too common).
    """
    if not name or len(name) < 3:
        return ContactMatch()

    # Full-text query — Gmail matches across body and header name fields.
    # Quoted-phrase queries ("Tom Peffer") return 0 via the JSON param
    # path even when the unquoted form finds hundreds of hits; Gmail
    # treats the quotes literally. We use the unquoted form and rely on
    # the all-tokens-in-display-name post-filter below to keep noise out.
    query = name.strip()

    try:
        proc = subprocess.run(
            [
                "gws", "gmail", "users", "messages", "list",
                "--params", json.dumps({
                    "userId": "me",
                    "q": query,
                    "maxResults": max_results,
                }),
            ],
            capture_output=True,
            timeout=_GMAIL_TIMEOUT_SEC,
            text=True,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        log.debug("gmail enrichment skipped (%s)", e)
        return ContactMatch()

    if proc.returncode != 0:
        log.debug("gmail list failed for %r: %s", name, proc.stderr[:200])
        return ContactMatch()

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return ContactMatch()

    messages = data.get("messages") or []
    if not messages:
        return ContactMatch()

    # Split the name into tokens so we can recognize a header display
    # field like "Tom Peffer <tp@foo.com>" even when Gmail's search hit
    # was actually on the body. First+last must both be present in the
    # display name for us to trust the email address.
    name_tokens = [t.lower() for t in re.split(r"\s+", name.strip()) if t]
    if not name_tokens:
        return ContactMatch()

    from collections import Counter
    address_counts: Counter[str] = Counter()

    # Dedupe by threadId so a 20-message thread doesn't inflate the count
    seen_threads: set[str] = set()
    for msg in messages[:max_results]:
        tid = msg.get("threadId")
        if tid and tid in seen_threads:
            continue
        if tid:
            seen_threads.add(tid)
        mid = msg.get("id")
        if not mid:
            continue
        try:
            # Use format=full — format=metadata with metadataHeaders
            # comes back empty through the gws wrapper.
            get_proc = subprocess.run(
                [
                    "gws", "gmail", "users", "messages", "get",
                    "--params", json.dumps({
                        "userId": "me",
                        "id": mid,
                        "format": "full",
                    }),
                ],
                capture_output=True,
                timeout=_GMAIL_TIMEOUT_SEC,
                text=True,
            )
            if get_proc.returncode != 0:
                continue
            msg_data = json.loads(get_proc.stdout)
            headers = (msg_data.get("payload") or {}).get("headers") or []
            for h in headers:
                if h.get("name", "").lower() not in ("from", "to", "cc"):
                    continue
                value = h.get("value", "")
                # Split address list on commas; each entry is "Display <addr>"
                for entry in value.split(","):
                    entry = entry.strip()
                    if not entry:
                        continue
                    lower = entry.lower()
                    # Require every token of the target name to appear in
                    # this entry's display portion — avoids cc'd strangers
                    if not all(tok in lower for tok in name_tokens):
                        continue
                    m = _EMAIL_HEADER_RE.search(entry)
                    if m:
                        address_counts[m.group(1).lower()] += 1
        except (subprocess.TimeoutExpired, json.JSONDecodeError):
            continue

    if not address_counts:
        return ContactMatch()

    # Top-ranked address first. Break ties by alphabetical order for stability.
    ranked = sorted(address_counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return ContactMatch(emails=[addr for addr, _ in ranked], sources=["gmail"])


# ---------------------------------------------------------------------------
# Frontmatter merge
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"^(---\s*\n)(.*?)(\n---\s*\n)(.*)$", re.DOTALL)


def _normalize_phone_for_compare(p: str) -> str:
    """Digits-only, last-10 for US numbers.

    Matches the rule in ``deja.observations.contacts._normalize_phone`` so
    ``+1 415 555 1234`` and ``(415) 555-1234`` compare equal. International
    numbers under 10 digits are kept verbatim.
    """
    digits = re.sub(r"\D", "", p)
    return digits[-10:] if len(digits) > 10 else digits


def _coerce_list(raw) -> list[str]:
    """Normalize a frontmatter value to a list of stripped strings.

    Accepts ``None`` (legacy missing field), a single string (legacy
    singular form), or a list. Returns an empty list for unrecognized
    shapes so downstream code never has to branch on type.
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw.strip()] if raw.strip() else []
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    return []


def _merge_contact_fields(
    frontmatter: dict,
    match: ContactMatch,
) -> tuple[dict, PageEnrichment]:
    """Apply a ContactMatch to a frontmatter dict.

    Contact fields are always list-valued (``emails:``, ``phones:``) so a
    person can have multiple addresses and numbers. Legacy singular forms
    (``email:``, ``phone:``) are migrated to the plural on first touch.

    Never removes existing values. Appends new ones that aren't already
    present after normalization (lowercased email, last-10-digits phone).
    Also migrates deprecated singular keys to plural list form.
    """
    result = dict(frontmatter)
    change = PageEnrichment(slug="", sources=list(match.sources))

    # ---- Emails: always a list ------------------------------------------
    existing_emails = _coerce_list(result.get("emails"))
    # Migrate legacy singular "email:" key if present
    legacy_email = _coerce_list(result.pop("email", None))
    for e in legacy_email:
        if e.lower() not in {x.lower() for x in existing_emails}:
            existing_emails.append(e)

    merged_emails = list(existing_emails)
    existing_email_keys = {e.lower() for e in merged_emails}
    for e in match.emails:
        key = e.strip().lower()
        if not key or key in existing_email_keys:
            continue
        merged_emails.append(e)
        existing_email_keys.add(key)
        change.added_emails.append(e)

    if merged_emails:
        result["emails"] = merged_emails

    # ---- Phones: always a list ------------------------------------------
    existing_phones = _coerce_list(result.get("phones"))
    legacy_phone = _coerce_list(result.pop("phone", None))
    for p in legacy_phone:
        norm = _normalize_phone_for_compare(p)
        if norm and norm not in {_normalize_phone_for_compare(x) for x in existing_phones}:
            existing_phones.append(p)

    merged_phones = list(existing_phones)
    existing_phone_norms = {_normalize_phone_for_compare(p) for p in merged_phones}
    existing_phone_norms.discard("")
    for p in match.phones:
        norm = _normalize_phone_for_compare(p)
        if not norm or norm in existing_phone_norms:
            continue
        merged_phones.append(p)
        existing_phone_norms.add(norm)
        change.added_phones.append(p)

    if merged_phones:
        result["phones"] = merged_phones

    # ---- Company: single-valued, don't overwrite ------------------------
    if match.company and not str(result.get("company", "")).strip():
        result["company"] = match.company
        change.added_company = match.company

    return result, change


def _apply_enrichment(text: str, match: ContactMatch) -> tuple[str, PageEnrichment]:
    """Return (rewritten_text, PageEnrichment) for a single markdown page.

    Preserves body content and non-contact frontmatter fields verbatim. If
    the page has no frontmatter block, a new one is added at the top.
    """
    m = _FRONTMATTER_RE.match(text)
    if m:
        open_fence, yaml_str, close_fence, body = m.groups()
        try:
            frontmatter = yaml.safe_load(yaml_str) or {}
            if not isinstance(frontmatter, dict):
                frontmatter = {}
        except Exception:
            frontmatter = {}
    else:
        frontmatter = {}
        body = text
        open_fence = "---\n"
        close_fence = "---\n\n"

    merged, change = _merge_contact_fields(frontmatter, match)

    # Nothing to add — return original text byte-for-byte
    if not (change.added_emails or change.added_phones or change.added_company):
        return text, change

    new_yaml = yaml.safe_dump(
        merged,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    ).rstrip()
    new_text = f"---\n{new_yaml}\n---\n{body if m else body.lstrip(chr(10))}"
    if not m:
        new_text = f"---\n{new_yaml}\n---\n\n{body.lstrip(chr(10))}"
    return new_text, change


# ---------------------------------------------------------------------------
# Top-level driver
# ---------------------------------------------------------------------------

def _title_from_body(text: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def enrich_people_pages(
    wiki_dir: Path | None = None,
    *,
    use_gmail: bool = True,
) -> EnrichmentReport:
    """Walk every ``people/*.md`` page and merge in contact info.

    For each page:
      1. Parse existing frontmatter + title
      2. Look up in macOS Contacts by title/aliases
      3. If still missing an email and ``use_gmail`` is set, try Gmail
      4. Merge results into frontmatter (never overwrite existing values)
      5. Write back if anything changed

    Returns an EnrichmentReport summarizing all changes.
    """
    if wiki_dir is None:
        wiki_dir = WIKI_DIR

    report = EnrichmentReport()
    people_dir = wiki_dir / "people"
    if not people_dir.is_dir():
        return report

    for path in sorted(people_dir.glob("*.md")):
        if path.name.startswith((".", "_")):
            continue
        report.pages_scanned += 1
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        # Skip the user's own self-page — enriching it from their own
        # Gmail/Contacts would produce noisy circular results and the
        # self-page's identity fields are set by the user directly.
        m = _FRONTMATTER_RE.match(text)
        fm = {}
        if m:
            try:
                fm = yaml.safe_load(m.group(2)) or {}
                if not isinstance(fm, dict):
                    fm = {}
            except Exception:
                fm = {}
        if fm.get("self") is True:
            continue

        title = _title_from_body(text) or path.stem.replace("-", " ").title()
        aliases_raw = fm.get("aliases") or []
        if isinstance(aliases_raw, str):
            aliases = [aliases_raw]
        elif isinstance(aliases_raw, list):
            aliases = [str(a) for a in aliases_raw]
        else:
            aliases = []

        # Source 1 — macOS Contacts (authoritative, cheap)
        match = lookup_macos_contact(title, aliases)
        if match.ambiguous:
            report.ambiguous.append(path.stem)
            log.info("contact_enrich: %s — ambiguous macos match, skipping", path.stem)
            continue

        # Source 2 — Gmail, only if we still don't have an email
        if use_gmail and not match.emails:
            existing_email = str(fm.get("email", "")).strip()
            if not existing_email:
                gmail_match = lookup_gmail_for_name(title)
                if gmail_match.emails:
                    match.emails.extend(gmail_match.emails)
                    match.sources.append("gmail")

        if match.is_empty():
            continue

        new_text, change = _apply_enrichment(text, match)
        change.slug = path.stem
        change.sources = list(match.sources)
        if change.added_emails or change.added_phones or change.added_company:
            try:
                path.write_text(new_text, encoding="utf-8")
                report.pages_changed += 1
                report.changes.append(change)
                log.info("contact_enrich: %s", change.brief())
            except OSError as e:
                log.warning("contact_enrich: failed to write %s: %s", path, e)

    return report
