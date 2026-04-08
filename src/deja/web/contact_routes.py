"""GET /api/contacts/search — @mention autocomplete."""

from __future__ import annotations

from fastapi import APIRouter, Query

router = APIRouter()


@router.get("/api/contacts/search")
def search_contacts(q: str = Query(""), limit: int = Query(10)) -> list[dict]:
    if not q:
        return []
    from deja.observations import contacts as contacts_mod

    if contacts_mod._name_set is None:
        contacts_mod._build_index()
    names = contacts_mod._name_set or set()
    phones = contacts_mod._phone_index or {}

    query = q.lower().strip()
    matches: list[dict] = []
    seen: set[str] = set()
    for name in sorted(names):
        if query in name and name not in seen:
            matching_phones = [p for p, n in phones.items() if n.lower() == name]
            matches.append(
                {
                    "name": name.title(),
                    "phones": matching_phones[:2],
                    "emails": [],
                    "goals": [],
                }
            )
            seen.add(name)
            if len(matches) >= limit:
                break
    return matches
