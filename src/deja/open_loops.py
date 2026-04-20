"""Open-loop candidate generator for the ``find_open_loops_with_evidence``
MCP tool.

For each open ``- [ ]`` item under ``## Tasks`` or ``## Waiting for`` in
goals.md, scan the last few days of event pages and return any that
keyword-match the open item. Pure parsing + substring matching — no
LLM, no writes. Cos decides whether the evidence actually closes the
loop and invokes ``complete_task`` / ``resolve_waiting_for`` directly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from deja import goals
from deja.config import WIKI_DIR


_UNCHECKED_RE = re.compile(r"^\s*-\s+\[\s\]\s+(.*)$")
_ADDED_SUFFIX_RE = re.compile(r"\s*\(added \d{4}-\d{2}-\d{2}\)\s*$")
_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n?(.*)$", re.DOTALL)

_KINDS = ("task", "waiting")
_SECTION_BY_KIND = {"task": "Tasks", "waiting": "Waiting for"}

# Default window for candidate event evidence.
_LOOKBACK_DAYS = 2
_EVENT_SNIPPET_CHARS = 400

# Shortest word length worth matching on. "the", "a", "of" etc would
# match every event and bury real signal.
_KEYWORD_MIN_LEN = 4

# Common filler tokens to strip before keywording. Not exhaustive —
# goal lines are usually noun phrases, so we catch verbs / dates /
# formatting leftovers.
_KEYWORD_STOPWORDS = {
    "from", "about", "with", "have", "does", "needs", "need", "this",
    "that", "their", "them", "they", "when", "will", "would", "could",
    "should", "back", "sent", "send", "tell", "told", "said", "gets",
    "getting", "taken", "took", "into", "over", "under", "before",
    "after", "while", "still", "open", "done", "item", "added",
    "today", "week", "month", "reply", "response", "confirm",
    "confirms", "confirmation", "please", "pleasant", "reminder",
}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class OpenItem:
    kind: str  # "task" | "waiting"
    text: str  # everything after "- [ ] "
    raw_line: str  # the full bullet line, including "- [ ] "


@dataclass
class EventEvidence:
    path: str  # "events/YYYY-MM-DD/slug.md"
    title: str
    people: list[str]
    projects: list[str]
    snippet: str
    matched_keywords: list[str] = field(default_factory=list)


@dataclass
class OpenLoopCandidate:
    open_item: OpenItem
    candidate_events: list[EventEvidence]
    reason_hints: list[str]


# ---------------------------------------------------------------------------
# Parse open items from goals.md
# ---------------------------------------------------------------------------


def parse_open_items() -> list[OpenItem]:
    """Return every open ``- [ ]`` bullet under Tasks + Waiting for."""
    goals_path = goals.GOALS_PATH
    if not goals_path.exists():
        return []
    try:
        text = goals_path.read_text(encoding="utf-8")
    except OSError:
        return []
    _, sections = goals._parse_sections(text)
    out: list[OpenItem] = []
    for kind in _KINDS:
        section_name = _SECTION_BY_KIND[kind]
        lines = sections.get(section_name, []) or []
        for line in lines:
            m = _UNCHECKED_RE.match(line)
            if not m:
                continue
            body = m.group(1).strip()
            if not body:
                continue
            out.append(OpenItem(kind=kind, text=body, raw_line=line))
    return out


# ---------------------------------------------------------------------------
# Keyword extraction
# ---------------------------------------------------------------------------


def _extract_keywords(text: str) -> list[str]:
    """Return distinctive lowercase keywords from an open-item line.

    Strips the ``(added YYYY-MM-DD)`` suffix, markdown emphasis, and
    link syntax; keeps tokens >= _KEYWORD_MIN_LEN that aren't in the
    stopword set. Names wrapped in ``[[slug|Person]]`` contribute both
    the slug form and the display form. Order is preserved (for
    deterministic matched_keywords output).
    """
    clean = _ADDED_SUFFIX_RE.sub("", text)
    # Replace wikilink forms with their display text + slug forms.
    # [[slug|Display Name]] -> "Display Name slug"
    # [[slug]] -> "slug"
    def _linkify(m: re.Match) -> str:
        inner = m.group(1)
        if "|" in inner:
            slug, display = inner.split("|", 1)
            return f"{display} {slug.replace('-', ' ')}"
        return inner.replace("-", " ")

    clean = re.sub(r"\[\[([^\]]+)\]\]", _linkify, clean)
    # Strip markdown formatting.
    clean = re.sub(r"[*_`~]+", " ", clean)
    # Normalize punctuation.
    clean = re.sub(r"[^\w\s-]", " ", clean)
    tokens = clean.lower().split()

    seen: set[str] = set()
    out: list[str] = []
    for tok in tokens:
        tok = tok.strip("-_")
        if len(tok) < _KEYWORD_MIN_LEN:
            continue
        if tok in _KEYWORD_STOPWORDS:
            continue
        if tok in seen:
            continue
        seen.add(tok)
        out.append(tok)
    return out


# ---------------------------------------------------------------------------
# Event loading
# ---------------------------------------------------------------------------


def _parse_event(path: Path) -> dict | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    fm_block = ""
    body = raw
    m = _FRONTMATTER_RE.match(raw)
    if m:
        fm_block = m.group(1)
        body = m.group(2)
    elif raw.startswith("---"):
        end = raw.find("---", 3)
        if end != -1:
            fm_block = raw[3:end]
            body = raw[end + 3 :]

    people: list[str] = []
    projects: list[str] = []
    pm = re.search(r"people:\s*\[([^\]]*)\]", fm_block)
    if pm:
        people = [s.strip() for s in pm.group(1).split(",") if s.strip()]
    prm = re.search(r"projects:\s*\[([^\]]*)\]", fm_block)
    if prm:
        projects = [s.strip() for s in prm.group(1).split(",") if s.strip()]

    title = path.stem
    for line in body.splitlines():
        if line.startswith("# "):
            title = line[2:].strip()
            break

    body_no_h1 = "\n".join(ln for ln in body.splitlines() if not ln.startswith("# "))
    snippet = re.sub(r"\s+", " ", body_no_h1).strip()[:_EVENT_SNIPPET_CHARS]

    try:
        rel = path.relative_to(WIKI_DIR).as_posix()
    except ValueError:
        rel = f"events/{path.parent.name}/{path.name}"

    return {
        "path": rel,
        "title": title,
        "people": people,
        "projects": projects,
        "snippet": snippet,
        "body_lower": (title + " " + snippet).lower(),
    }


def load_recent_events(days: int = _LOOKBACK_DAYS) -> list[dict]:
    """Return every event page whose date-dir falls in the last ``days`` days.

    Newest-first by date-dir. Each entry is a dict with keys path,
    title, people, projects, snippet, body_lower.
    """
    events_dir = WIKI_DIR / "events"
    if not events_dir.is_dir():
        return []

    today = date.today()
    cutoff = today - timedelta(days=max(0, days))
    events: list[dict] = []
    for day_dir in sorted(events_dir.iterdir(), reverse=True):
        if not day_dir.is_dir():
            continue
        try:
            d = date.fromisoformat(day_dir.name)
        except ValueError:
            continue
        if d < cutoff:
            continue
        for event_file in sorted(day_dir.glob("*.md")):
            ev = _parse_event(event_file)
            if ev is not None:
                events.append(ev)
    return events


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


MAX_EVENTS_PER_CANDIDATE = 8


def match_open_loops(
    days: int = _LOOKBACK_DAYS,
    limit: int = 20,
) -> list[OpenLoopCandidate]:
    """Return open-item/event candidate pairs worth inspecting.

    For each open item, any event in the ``days``-day window whose
    title+body contains at least TWO of the item's distinctive
    keywords (or one keyword >=7 chars) counts as evidence. The
    two-hit rule cuts false positives from common tokens like
    ``google`` or ``date`` matching unrelated items. The
    ``reason_hints`` list names the matching keywords so cos has a
    scent to follow via ``get_page``.

    Open items with zero evidence are omitted. Per candidate, the
    top ``MAX_EVENTS_PER_CANDIDATE`` best-scoring events are kept
    (ranked by number of matching keywords, then recency). ``limit``
    caps the number of open-item candidates returned.
    """
    open_items = parse_open_items()
    if not open_items:
        return []
    events = load_recent_events(days=days)
    if not events:
        return []

    candidates: list[OpenLoopCandidate] = []
    for item in open_items:
        keywords = _extract_keywords(item.text)
        if not keywords:
            continue
        scored: list[tuple[int, EventEvidence]] = []
        matched_keywords_overall: list[str] = []
        for ev in events:
            hits = [kw for kw in keywords if kw in ev["body_lower"]]
            # Two-hit rule OR one long/specific keyword.
            if len(hits) < 2 and not any(len(kw) >= 7 for kw in hits):
                continue
            scored.append((
                len(hits),
                EventEvidence(
                    path=ev["path"],
                    title=ev["title"],
                    people=ev["people"],
                    projects=ev["projects"],
                    snippet=ev["snippet"],
                    matched_keywords=hits,
                ),
            ))
            for kw in hits:
                if kw not in matched_keywords_overall:
                    matched_keywords_overall.append(kw)
        if not scored:
            continue
        scored.sort(key=lambda t: -t[0])
        matched_events = [ev for _, ev in scored[:MAX_EVENTS_PER_CANDIDATE]]
        candidates.append(
            OpenLoopCandidate(
                open_item=item,
                candidate_events=matched_events,
                reason_hints=matched_keywords_overall,
            )
        )
        if len(candidates) >= limit:
            break
    return candidates
