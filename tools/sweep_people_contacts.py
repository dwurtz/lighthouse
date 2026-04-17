"""Sweep every people/*.md page and reconcile frontmatter phones
against macOS Contacts + Google People API.

For each page with ``phones:`` entries:
  - Resolve each phone via macOS (existing ``observations.contacts``)
    and Google (via ``gws people people connections list``).
  - If the contacts source returns a name that doesn't match the page's
    h1 / preferred_name → flag as diff.
  - If contacts has a nickname we don't have in aliases → flag.
  - If the phone resolves to NOTHING → flag as unknown.

Dry-run by default; ``--apply`` rewrites pages + appends missing
aliases.

No LLM — pure deterministic reconciliation.

TODO(gws-migration): this dev-only tool still shells out to ``gws``
for the Google People lookup. Production code has moved to direct
``googleapiclient`` calls via ``deja.google_api`` — migrate this
script as a follow-up so ``gws`` isn't required on developer machines
either. Tracked as part of the gws-subprocess removal effort.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

try:
    import yaml
except ImportError:
    print("PyYAML required — run via `uv run`", file=sys.stderr)
    sys.exit(1)

from deja.observations.contacts import (  # noqa: E402
    resolve_contact,
    _normalize_phone,
)

WIKI = Path.home() / "Deja"
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)


# ---------------------------------------------------------------------------
# Google Contacts index (via gws) — merged lookup
# ---------------------------------------------------------------------------


def _fetch_google_contacts() -> list[dict]:
    """Paginate `gws people connections list` and return all connections."""
    all_conns: list[dict] = []
    page_token: str | None = None
    for _ in range(20):  # hard page cap — 20 × 1000 = 20K contacts
        params = {
            "resourceName": "people/me",
            "personFields": "names,phoneNumbers,emailAddresses,nicknames",
            "pageSize": 1000,
        }
        if page_token:
            params["pageToken"] = page_token
        result = subprocess.run(
            ["gws", "people", "people", "connections", "list",
             "--params", json.dumps(params), "--format", "json"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            print(f"gws failed: {result.stderr[:200]}", file=sys.stderr)
            break
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            print("gws returned non-JSON", file=sys.stderr)
            break
        all_conns.extend(data.get("connections", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return all_conns


@dataclass
class ContactMatch:
    source: str
    display_name: str
    nicknames: list[str] = field(default_factory=list)


def _google_index(conns: list[dict]) -> dict[str, ContactMatch]:
    """phone-last-10-digits → ContactMatch(google)."""
    idx: dict[str, ContactMatch] = {}
    for c in conns:
        names = c.get("names", [])
        if not names:
            continue
        display = names[0].get("displayName") or names[0].get("unstructuredName") or ""
        if not display:
            continue
        nicks = [n.get("value", "").strip() for n in c.get("nicknames", [])]
        nicks = [n for n in nicks if n]
        for p in c.get("phoneNumbers", []):
            raw = p.get("canonicalForm") or p.get("value")
            if not raw:
                continue
            key = _normalize_phone(raw)
            if key and key not in idx:
                idx[key] = ContactMatch("google", display, nicks)
    return idx


# ---------------------------------------------------------------------------
# Page reconciliation
# ---------------------------------------------------------------------------


@dataclass
class PageIssue:
    slug: str
    kind: str            # "name_mismatch" | "missing_alias" | "unresolved_phone"
    detail: str
    suggested: str = ""


def _parse_page(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return {}, text
    if not isinstance(fm, dict):
        return {}, text
    body = text[m.end():]
    return fm, body


def _h1(body: str) -> str:
    for line in body.splitlines():
        s = line.strip()
        if s.startswith("# "):
            return s[2:].strip()
    return ""


def sweep(google_idx: dict[str, ContactMatch]) -> list[PageIssue]:
    issues: list[PageIssue] = []
    people_dir = WIKI / "people"
    if not people_dir.is_dir():
        return issues

    for page in sorted(people_dir.glob("*.md")):
        fm, body = _parse_page(page)
        phones_raw = fm.get("phones") or []
        if isinstance(phones_raw, str):
            phones_raw = [phones_raw]
        if not phones_raw:
            continue

        h1 = _h1(body) or fm.get("preferred_name") or page.stem
        slug = page.stem
        aliases = fm.get("aliases") or []
        if isinstance(aliases, str):
            aliases = [aliases]
        aliases_lower = {a.strip().lower() for a in aliases}

        for raw_phone in phones_raw:
            if not isinstance(raw_phone, str):
                continue
            key = _normalize_phone(raw_phone)
            if not key:
                continue

            # Resolve via macOS first (uses existing contacts_buffer), then Google.
            mac_name = resolve_contact(raw_phone)
            g = google_idx.get(key)

            resolved_name: str | None = None
            resolved_nicks: list[str] = []
            source: str = ""
            if mac_name:
                resolved_name = mac_name
                source = "macos"
            elif g:
                resolved_name = g.display_name
                resolved_nicks = g.nicknames
                source = "google"

            if not resolved_name:
                issues.append(PageIssue(
                    slug, "unresolved_phone",
                    f"phone {raw_phone} not found in macOS or Google Contacts",
                ))
                continue

            # Name mismatch — compare by casefold, strip honorifics loosely.
            cmp_page = h1.casefold().strip()
            cmp_resolved = resolved_name.casefold().strip()
            if cmp_page != cmp_resolved and cmp_resolved not in cmp_page and cmp_page not in cmp_resolved:
                issues.append(PageIssue(
                    slug, "name_mismatch",
                    f"page says '{h1}', Contacts ({source}) says '{resolved_name}' for phone {raw_phone}",
                    suggested=resolved_name,
                ))

            # Missing aliases
            for nick in resolved_nicks:
                if nick.lower() not in aliases_lower:
                    issues.append(PageIssue(
                        slug, "missing_alias",
                        f"Google nickname '{nick}' not in aliases",
                        suggested=nick,
                    ))

    return issues


def apply_fixes(issues: list[PageIssue]) -> int:
    """Apply missing_alias additions + flag name_mismatch for user review.

    We auto-apply aliases only. Name mismatches require judgment (the
    page title might be intentionally a preferred shortening).
    """
    applied = 0
    by_slug: dict[str, list[PageIssue]] = {}
    for i in issues:
        by_slug.setdefault(i.slug, []).append(i)

    for slug, lst in by_slug.items():
        to_add = [i.suggested for i in lst
                  if i.kind == "missing_alias" and i.suggested]
        if not to_add:
            continue
        page = WIKI / "people" / f"{slug}.md"
        fm, body = _parse_page(page)
        if not fm:
            continue
        aliases = fm.get("aliases") or []
        if isinstance(aliases, str):
            aliases = [aliases]
        existing_lower = {a.strip().lower() for a in aliases}
        added = False
        for nick in to_add:
            if nick.lower() not in existing_lower:
                aliases.append(nick)
                existing_lower.add(nick.lower())
                added = True
        if not added:
            continue
        fm["aliases"] = aliases
        new_fm = yaml.safe_dump(fm, default_flow_style=False, sort_keys=False).rstrip()
        page.write_text(f"---\n{new_fm}\n---\n{body}", encoding="utf-8")
        applied += 1
    return applied


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="Apply missing-alias fixes (name mismatches always reported, never auto-applied)")
    args = ap.parse_args()

    print("Fetching Google contacts...", file=sys.stderr)
    conns = _fetch_google_contacts()
    print(f"  {len(conns)} Google contacts", file=sys.stderr)
    g_idx = _google_index(conns)
    print(f"  {len(g_idx)} Google phone mappings\n", file=sys.stderr)

    print("Sweeping people/*.md...", file=sys.stderr)
    issues = sweep(g_idx)
    by_kind: dict[str, list[PageIssue]] = {}
    for i in issues:
        by_kind.setdefault(i.kind, []).append(i)

    print(f"\n{len(issues)} issue(s):")
    print(f"  name_mismatch:     {len(by_kind.get('name_mismatch', []))}")
    print(f"  missing_alias:     {len(by_kind.get('missing_alias', []))}")
    print(f"  unresolved_phone:  {len(by_kind.get('unresolved_phone', []))}")
    print()

    for kind in ("name_mismatch", "missing_alias", "unresolved_phone"):
        if not by_kind.get(kind):
            continue
        print(f"=== {kind} ===")
        for i in by_kind[kind]:
            print(f"  [{i.slug}] {i.detail}")
            if i.suggested:
                print(f"    → suggested: {i.suggested!r}")
        print()

    if args.apply:
        applied = apply_fixes(issues)
        print(f"\nApplied alias additions to {applied} page(s).")
        print("Name mismatches reported only — not auto-applied.")
    else:
        print("\nDry run. Re-run with --apply to auto-add missing aliases.")
        print("Name mismatches always require manual review.")


if __name__ == "__main__":
    main()
