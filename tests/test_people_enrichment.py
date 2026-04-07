"""Contact enrichment — merge rules, frontmatter preservation, ambiguity handling.

Network sources (macOS Contacts, gws gmail) are stubbed out; these tests
lock the merge logic and the page-rewriting contract, which is where the
risk lives. The real DB and gws paths are covered by the live run.
"""

from __future__ import annotations

from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Frontmatter merge — the core safety-critical logic
# ---------------------------------------------------------------------------

def test_merge_adds_email_as_list():
    """Contact fields are always list-valued — supports multiple per person."""
    from deja.people_enrichment import ContactMatch, _merge_contact_fields
    fm = {"keywords": ["soccer"]}
    match = ContactMatch(emails=["jane@example.com"], sources=["macos"])
    merged, change = _merge_contact_fields(fm, match)
    assert merged["emails"] == ["jane@example.com"]
    # Singular "email" key is never written
    assert "email" not in merged
    assert change.added_emails == ["jane@example.com"]
    # Non-contact fields are untouched
    assert merged["keywords"] == ["soccer"]


def test_merge_appends_multiple_emails():
    from deja.people_enrichment import ContactMatch, _merge_contact_fields
    fm = {"emails": ["jane@old.com"]}
    match = ContactMatch(emails=["jane@new.com", "jane@old.com"], sources=["macos"])
    merged, change = _merge_contact_fields(fm, match)
    # Duplicate skipped, new one appended
    assert merged["emails"] == ["jane@old.com", "jane@new.com"]
    assert change.added_emails == ["jane@new.com"]


def test_merge_migrates_legacy_singular_email_to_list():
    """Pages from before this change used ``email: foo@bar.com``; first
    touch should migrate them to ``emails: [foo@bar.com]``."""
    from deja.people_enrichment import ContactMatch, _merge_contact_fields
    fm = {"email": "jane@legacy.com"}
    match = ContactMatch(emails=["jane@new.com"], sources=["macos"])
    merged, change = _merge_contact_fields(fm, match)
    assert "email" not in merged
    assert merged["emails"] == ["jane@legacy.com", "jane@new.com"]


def test_merge_appends_new_phone_only():
    from deja.people_enrichment import ContactMatch, _merge_contact_fields
    fm = {"phones": ["+1 415 555 1234"]}
    match = ContactMatch(
        phones=["(415) 555-1234", "+1 212 555 9999"],  # first is a duplicate (normalized)
        sources=["macos"],
    )
    merged, change = _merge_contact_fields(fm, match)
    assert len(merged["phones"]) == 2
    assert "+1 212 555 9999" in merged["phones"]
    # The duplicate (same digits) was NOT re-added
    assert merged["phones"].count("+1 415 555 1234") == 1
    assert change.added_phones == ["+1 212 555 9999"]


def test_merge_migrates_legacy_singular_phone_to_list():
    from deja.people_enrichment import ContactMatch, _merge_contact_fields
    fm = {"phone": "+14155551234"}
    match = ContactMatch(phones=["+12125559999"], sources=["macos"])
    merged, change = _merge_contact_fields(fm, match)
    assert "phone" not in merged
    assert merged["phones"] == ["+14155551234", "+12125559999"]


def test_merge_single_phone_still_uses_list_form():
    """Even a single phone is stored as a list — no singular special case."""
    from deja.people_enrichment import ContactMatch, _merge_contact_fields
    fm = {}
    match = ContactMatch(phones=["+14155551234"], sources=["macos"])
    merged, change = _merge_contact_fields(fm, match)
    assert merged["phones"] == ["+14155551234"]
    assert "phone" not in merged


def test_merge_adds_company_when_missing():
    from deja.people_enrichment import ContactMatch, _merge_contact_fields
    fm = {}
    match = ContactMatch(company="Acme Corp", sources=["macos"])
    merged, change = _merge_contact_fields(fm, match)
    assert merged["company"] == "Acme Corp"
    assert change.added_company == "Acme Corp"


def test_merge_never_overwrites_existing_company():
    from deja.people_enrichment import ContactMatch, _merge_contact_fields
    fm = {"company": "Legacy Inc"}
    match = ContactMatch(company="New Co", sources=["macos"])
    merged, change = _merge_contact_fields(fm, match)
    assert merged["company"] == "Legacy Inc"
    assert change.added_company == ""


def test_merge_empty_match_no_changes():
    from deja.people_enrichment import ContactMatch, _merge_contact_fields
    fm = {"keywords": ["soccer"]}
    match = ContactMatch()
    merged, change = _merge_contact_fields(fm, match)
    assert merged == fm
    assert not (change.added_emails or change.added_phones or change.added_company)


# ---------------------------------------------------------------------------
# Full page rewrite — preserves body and frontmatter ordering
# ---------------------------------------------------------------------------

def test_apply_enrichment_preserves_body_verbatim():
    from deja.people_enrichment import ContactMatch, _apply_enrichment
    text = (
        "---\n"
        "keywords: [soccer, parent]\n"
        "aliases:\n"
        "  - Jane\n"
        "  - Janey\n"
        "---\n"
        "# Jane Doe\n"
        "\n"
        "Jane is the mother of [[ruby]]'s teammate.\n"
    )
    match = ContactMatch(emails=["jane@example.com"], sources=["macos"])
    new_text, change = _apply_enrichment(text, match)
    # Body is untouched
    assert "# Jane Doe" in new_text
    assert "Jane is the mother of [[ruby]]'s teammate." in new_text
    # Emails list added to frontmatter
    assert "jane@example.com" in new_text
    assert "emails:" in new_text
    # Pre-existing fields are preserved
    assert "keywords" in new_text
    assert "aliases" in new_text
    assert change.added_emails == ["jane@example.com"]


def test_apply_enrichment_migrates_legacy_email_and_appends():
    """A page with legacy ``email: foo`` gets migrated to the plural form
    and the new address is appended."""
    from deja.people_enrichment import ContactMatch, _apply_enrichment
    text = (
        "---\n"
        "email: existing@set.com\n"
        "---\n"
        "# Jane\n\nHi.\n"
    )
    match = ContactMatch(emails=["new@example.com"], sources=["macos"])
    new_text, change = _apply_enrichment(text, match)
    # Migration happened — no more singular form
    assert "email: existing" not in new_text
    assert "existing@set.com" in new_text
    assert "new@example.com" in new_text
    assert "emails:" in new_text
    assert change.added_emails == ["new@example.com"]


def test_apply_enrichment_creates_frontmatter_when_missing():
    from deja.people_enrichment import ContactMatch, _apply_enrichment
    text = "# Jane Doe\n\nA person with no frontmatter.\n"
    match = ContactMatch(emails=["jane@example.com"], sources=["macos"])
    new_text, change = _apply_enrichment(text, match)
    assert new_text.startswith("---\n")
    assert "jane@example.com" in new_text
    assert "emails:" in new_text
    assert "# Jane Doe" in new_text
    assert "A person with no frontmatter." in new_text


# ---------------------------------------------------------------------------
# Name-candidate generation — what gets matched against Contacts
# ---------------------------------------------------------------------------

def test_name_candidates_includes_title_and_aliases():
    from deja.people_enrichment import _name_candidates
    cands = _name_candidates("Jane Doe", ["Janey", "JD"])
    assert "jane doe" in cands
    assert "janey" in cands
    assert "jd" in cands


def test_name_candidates_strips_parenthetical():
    """Page title 'Justin (Molly's Dad)' should also match plain 'Justin'
    as a fallback — but the full form wins if both are in Contacts."""
    from deja.people_enrichment import _name_candidates
    cands = _name_candidates("Justin (Molly's Dad)", [])
    assert "justin (molly's dad)" in cands
    assert "justin" in cands


# ---------------------------------------------------------------------------
# Ambiguity handling — don't guess between multiple matches
# ---------------------------------------------------------------------------

def test_lookup_macos_ambiguous_skips(monkeypatch):
    from deja import people_enrichment as ce
    fake_contacts = [
        {"full_name": "Justin Smith", "first": "Justin", "last": "Smith",
         "org": "Acme", "nickname": "", "phones": ["555-1111"], "emails": ["js@a.com"]},
        {"full_name": "Justin Brown", "first": "Justin", "last": "Brown",
         "org": "Beta", "nickname": "", "phones": ["555-2222"], "emails": ["jb@b.com"]},
    ]
    monkeypatch.setattr(ce, "_macos_cache", fake_contacts)
    match = ce.lookup_macos_contact("Justin", [])
    assert match.ambiguous
    assert match.emails == []
    assert match.phones == []


def test_lookup_macos_unique_full_name_match(monkeypatch):
    from deja import people_enrichment as ce
    fake = [
        {"full_name": "Jane Doe", "first": "Jane", "last": "Doe",
         "org": "Acme Corp", "nickname": "Janey",
         "phones": ["+14155551234"], "emails": ["jane@example.com"]},
        {"full_name": "Bob Jones", "first": "Bob", "last": "Jones",
         "org": "", "nickname": "", "phones": [], "emails": []},
    ]
    monkeypatch.setattr(ce, "_macos_cache", fake)
    match = ce.lookup_macos_contact("Jane Doe", [])
    assert not match.ambiguous
    assert match.emails == ["jane@example.com"]
    assert match.phones == ["+14155551234"]
    assert match.company == "Acme Corp"
    assert "macos" in match.sources


def test_lookup_macos_alias_match(monkeypatch):
    from deja import people_enrichment as ce
    fake = [{
        "full_name": "Robert Toy", "first": "Robert", "last": "Toy",
        "org": "", "nickname": "Coach Rob",
        "phones": [], "emails": ["rob@soccer.com"],
    }]
    monkeypatch.setattr(ce, "_macos_cache", fake)
    # Page title "Coach Rob" matches via nickname
    match = ce.lookup_macos_contact("Coach Rob", [])
    assert match.emails == ["rob@soccer.com"]


def test_lookup_macos_no_match_returns_empty(monkeypatch):
    from deja import people_enrichment as ce
    monkeypatch.setattr(ce, "_macos_cache", [])
    match = ce.lookup_macos_contact("Nobody Special", [])
    assert match.is_empty()
    assert not match.ambiguous


# ---------------------------------------------------------------------------
# enrich_people_pages — end-to-end on a tmp wiki
# ---------------------------------------------------------------------------

def test_enrich_people_pages_writes_changes(isolated_home, monkeypatch):
    _, wiki = isolated_home
    import deja.people_enrichment as ce
    monkeypatch.setattr(ce, "WIKI_DIR", wiki)
    # Stub out Gmail so we don't hit the network
    monkeypatch.setattr(ce, "lookup_gmail_for_name",
                        lambda name, **kw: ce.ContactMatch())
    # Fake macOS Contacts with one matching person
    monkeypatch.setattr(ce, "_macos_cache", [
        {"full_name": "Jane Doe", "first": "Jane", "last": "Doe",
         "org": "Acme Corp", "nickname": "",
         "phones": ["+14155551234"], "emails": ["jane@example.com"]},
    ])

    (wiki / "people").mkdir()
    (wiki / "people" / "jane-doe.md").write_text(
        "---\nkeywords: [parent]\n---\n# Jane Doe\n\nA person.\n"
    )
    (wiki / "people" / "unknown.md").write_text("# Unknown\n\nNo contacts match.\n")

    report = ce.enrich_people_pages(use_gmail=False)
    assert report.pages_scanned == 2
    assert report.pages_changed == 1
    assert report.changes[0].slug == "jane-doe"

    jane_text = (wiki / "people" / "jane-doe.md").read_text()
    assert "jane@example.com" in jane_text
    assert "emails:" in jane_text
    assert "+14155551234" in jane_text
    assert "phones:" in jane_text
    assert "company: Acme Corp" in jane_text
    # Pre-existing fields preserved
    assert "keywords" in jane_text


def test_enrich_people_pages_skips_self(isolated_home, monkeypatch):
    _, wiki = isolated_home
    import deja.people_enrichment as ce
    monkeypatch.setattr(ce, "WIKI_DIR", wiki)
    monkeypatch.setattr(ce, "_macos_cache", [
        {"full_name": "Me Myself", "first": "Me", "last": "Myself",
         "org": "", "nickname": "",
         "phones": ["+1 999"], "emails": ["me@me.com"]},
    ])
    (wiki / "people").mkdir()
    (wiki / "people" / "me.md").write_text(
        "---\nself: true\nemail: existing@me.com\n---\n# Me Myself\n\nBio.\n"
    )
    report = ce.enrich_people_pages(use_gmail=False)
    assert report.pages_changed == 0
    # Self-page left untouched
    assert "existing@me.com" in (wiki / "people" / "me.md").read_text()


def test_enrich_people_pages_is_idempotent(isolated_home, monkeypatch):
    _, wiki = isolated_home
    import deja.people_enrichment as ce
    monkeypatch.setattr(ce, "WIKI_DIR", wiki)
    monkeypatch.setattr(ce, "lookup_gmail_for_name",
                        lambda name, **kw: ce.ContactMatch())
    monkeypatch.setattr(ce, "_macos_cache", [
        {"full_name": "Jane Doe", "first": "Jane", "last": "Doe",
         "org": "", "nickname": "",
         "phones": [], "emails": ["jane@example.com"]},
    ])
    (wiki / "people").mkdir()
    (wiki / "people" / "jane-doe.md").write_text("# Jane Doe\n\nHi.\n")

    r1 = ce.enrich_people_pages(use_gmail=False)
    r2 = ce.enrich_people_pages(use_gmail=False)
    assert r1.pages_changed == 1
    assert r2.pages_changed == 0
