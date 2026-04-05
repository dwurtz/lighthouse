"""Focused analysis context — index.md + hybrid BM25/vector retrieval.

Replaces the `render_for_prompt()` full-wiki dump in the hot analysis
path. The analysis cycle calls `build_analysis_context(signal_items)`
which:

  1. Always includes `index.md` (compact catalog so the model sees
     every slug even when retrieval whiffs).
  2. Runs **two retrievers in parallel** against the signal batch:
       a. **BM25 on extracted entity tokens** — proper nouns,
          multi-word name phrases, and domain stems pulled out of
          each signal with cheap regex. BM25 is fast (~100ms per
          call) and excellent at exact keyword match.
       b. **Vector on the concatenated signal text** — one
          embedding-based call that catches semantic matches where
          keywords don't overlap (e.g. "Zillow listing Palo Alto" →
          `projects/palo-alto-relocation`). Requires `qmd embed` to
          have been run.
  3. Merges results with a minimum-score cut, reads matched pages
     from disk, and returns focused context.

## Why both retrievers, not one

They have complementary failure modes — each catches what the other
misses on the canonical eight-case benchmark:

- **BM25's strength:** exact keyword matching. "Tom Peffer" → `tom-peffer.md`
  deterministically in ~100ms. Fast, zero setup, no embedding model needed.
- **BM25's weakness:** long natural-language queries over-rank meta
  files (`index.md`, `log.md` contain every entity). You have to feed
  it short entity-shaped queries, not raw signal prose.
- **Vector's strength:** semantic bridging when the signal doesn't
  mention the slug verbatim. "configuring a Shopify theme for kids
  clothing" → the Blade & Rose page (provided the page actually says
  so — see wiki-content caveat below).
- **Vector's weakness:** flat score distributions on weak-signal
  queries, and sensitivity to the 300M embedding model's world
  knowledge gaps.

An earlier version of this module ran BM25 on full concatenated signal
text and was silently producing ∅ because every long query hit only
meta-files. The fix is NOT to remove BM25 but to give it entity tokens
instead of prose. See `_extract_entity_tokens`.

## Safety net

If both retrievers return nothing AND the wiki is effectively empty,
fall back to the full wiki dump (only triggers on fresh installs).
For everything else, the always-included `index.md` catalog plus the
"other pages available" footer give Flash Lite enough signal to name
any slug in its output even if retrieval whiffed — so a missed fetch
is never a blind spot.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from lighthouse import wiki as wiki_store
from lighthouse.config import WIKI_DIR
from lighthouse.llm.search import search_files, vsearch_files
from lighthouse.wiki_catalog import render_index_for_prompt

log = logging.getLogger(__name__)

# Files that reference every entity and would drown out real page hits.
# Includes the wiki root's meta-files (index, log, nightly notes) plus
# the prompts/ directory — prompt templates routinely contain example
# wiki entity names and would otherwise rank highly on any query.
_META_BASENAMES = {
    "index.md",
    "log.md",
    "nightly.md",
    "claude.md",
    "how-it-works.md",
    # Prompt files — every prompt mentions wiki entities as examples.
    "describe_screen.md",
    "chat.md",
    "analyze-write.md",
    "nightly-cleanup.md",
    "triage-message.md",
    "prefilter.md",
}

# Categories we actually want to retrieve from. Anything else (prompts/,
# .backups/, root-level files) is filtered out even if it scores well.
_ALLOWED_CATEGORIES = ("people", "projects")

# Cap per-cycle prompt size regardless of signal volume. Keep this
# tight — including low-confidence pages actively misleads Flash (e.g.
# "tinker-desk-organizers" ranking #1 for "child's outfit e-commerce"
# because both documents mention "configuring"). Fewer, higher-quality
# pages beat a big noisy dump.
_MAX_PAGES = 6

# Minimum similarity score for a page to make it into the prompt.
# Empirically tuned against realistic signal batches on the 300M
# embeddinggemma model by inspecting score distributions:
#
#   • Strong-signal queries (clear entity mentions) produce top hits
#     in the 0.65-0.75 range — obviously above threshold.
#   • Weak-signal queries (no entity names, generic prose) cluster
#     results around 0.50-0.56 — the top hit is usually wrong.
#
# 0.58 is the elbow: captures clean matches down to ~0.60 with a
# little headroom for float precision while dropping the 0.50-0.55
# noise band. Returning nothing is strictly better than returning a
# confident false positive — the always-included index.md footer
# still exposes every slug to Flash, so a retrieval miss is never a
# blind spot.
_MIN_SCORE = 0.58

# Sources whose `sender` field carries discriminative information the
# embedder can use (email addresses contain names, iMessage/WhatsApp
# senders are contacts). For everything else (calendar, chrome,
# screenshot, tasks, drive), the sender is a system ID and adding it
# to the query only dilutes the semantics — empirically dropped hits
# from 0.61 to 0.60 on calendar queries and from 0.55 to 0.53 on
# chrome URL queries.
_INFORMATIVE_SENDER_SOURCES = {"email", "imessage", "whatsapp"}

# BM25 per-token config. BM25 scores are on a different scale than
# cosine vector scores — 0.80+ is routine for clean term matches, and
# "pure noise" queries still return 0.55-0.65 hits on meta files.
# 0.70 is the sweet spot: it keeps every clean entity match and drops
# the partial-token noise band.
_BM25_MIN_SCORE = 0.70
_BM25_PER_TOKEN_LIMIT = 5

# Vector search result budget per call — fetch more than `_MAX_PAGES`
# so score filtering + meta-file filtering still leave us with a full
# set of candidates above the threshold.
_VSEARCH_CANDIDATES = 15

# Query length cap. Vector encoders handle long text, but qmd's HyDE
# expansion (`qmd query`) and its internal reranker scale with query
# length — long concatenations blow latency from 3s to 15s+ per cycle.
# Keep the retrieval query tight: it only needs to be semantically
# representative of the batch, not contain every signal verbatim.
_MAX_QUERY_CHARS = 400


# Capitalized words or multi-word proper-noun phrases. Handles names
# ("Tom Peffer"), places ("Palo Alto"), and brand fragments ("Blade",
# "Rose"). Deliberately greedy — we'd rather query BM25 five times
# with loose candidates than miss the one phrase that matches.
_PROPER_NOUN_RE = re.compile(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,3}\b")

# Host stem from a URL — "bladeandrose.com" → "bladeandrose". Domain
# stems are surprisingly good BM25 queries because project pages
# often mention the domain verbatim.
_DOMAIN_RE = re.compile(r"\b([a-z][a-z0-9-]{2,})\.(?:com|org|io|co|net|app|dev|ai)\b")

# Common capitalized words that aren't entities — strip these from
# proper-noun extraction to cut noise.
_STOP_TOKENS = {
    "meeting", "email", "text", "message", "calendar", "chrome",
    "david", "hey", "david's", "from", "re", "fwd", "david wurtz",
    "the", "this", "that", "monday", "tuesday", "wednesday",
    "thursday", "friday", "saturday", "sunday", "january", "february",
    "march", "april", "may", "june", "july", "august", "september",
    "october", "november", "december", "am", "pm",
}


def _extract_entity_tokens(signal_items: list[dict]) -> list[str]:
    """Pull short entity-shaped tokens out of the signal batch for BM25.

    Extracts:
      - Proper-noun phrases ("Tom Peffer", "Palo Alto", "Blade Rose")
        from signal text AND sender fields
      - Domain stems from URLs ("bladeandrose", "alfredcapital")
      - Sender email local-parts ("mike@alfredcapital.com" → "alfredcapital")

    Deduplicated case-insensitive. Stop-tokens (common capitalized
    words that aren't entities) are filtered out.

    **Outbound signals (David's own messages) are iterated first.**
    The total token budget is capped at 10 per cycle, so extracting
    from outbound first means the people and projects David himself
    just mentioned claim the limited slots ahead of tokens pulled
    from promotional inbound.
    """
    from lighthouse.observations.types import is_outbound

    # Iterate outbound signals before inbound so their entity tokens
    # land in the output list ahead of inbound ones, before the
    # 10-token cap kicks in.
    ordered_items = sorted(
        signal_items,
        key=lambda d: 0 if is_outbound(d) else 1,
    )

    raw: list[str] = []

    for d in ordered_items:
        text = d.get("text") or ""
        sender = d.get("sender") or ""
        blob = f"{sender} {text}"

        # Proper-noun phrases
        for m in _PROPER_NOUN_RE.finditer(blob):
            raw.append(m.group(0))

        # Domain stems
        for m in _DOMAIN_RE.finditer(blob.lower()):
            raw.append(m.group(1))

        # Email local-parts and the second half (company domain stem)
        # "mike@alfredcapital.com" → "mike", "alfredcapital"
        for email_m in re.finditer(r"\b([a-z0-9._-]+)@([a-z0-9-]+)\.", blob.lower()):
            local = email_m.group(1)
            host = email_m.group(2)
            if len(local) >= 3:
                raw.append(local)
            if len(host) >= 4:
                raw.append(host)

    # Deduplicate case-insensitively, preserve order, filter stop tokens.
    seen: set[str] = set()
    out: list[str] = []
    for tok in raw:
        key = tok.lower().strip()
        if not key or key in seen or key in _STOP_TOKENS:
            continue
        if len(key) < 3:
            continue
        seen.add(key)
        out.append(tok)

    # Cap total queries per cycle — BM25 is fast but not free, and
    # diminishing returns past the first ~8 entity tokens.
    return out[:10]


def _build_query(signal_items: list[dict]) -> str:
    """Concatenate the batch's signals into one focused retrieval query.

    For message-like sources (email, imessage, whatsapp) we prepend the
    sender to the body — email addresses and contact names contain
    literal identity tokens (`mike@alfredcapital.com` → "alfred") that
    the embedder uses as strong discriminative signal, pulling the
    right person's page into the top results even when the body text
    itself is generic.

    For other sources (calendar, browser, screenshot, tasks, drive)
    the sender is a system ID (calendar id, "chrome", etc.) and adding
    it to the query actively dilutes semantics — verified empirically.
    So we include the body only.

    **Outbound signals are upweighted two ways.** They (1) appear
    first in the concatenated query and (2) get prefixed with the
    literal marker "David said:" which pulls the embedding vector
    toward content about what David is actively talking about. Every
    outbound message is also an open entry in the attention budget —
    the batch retrieval query should pull hardest on David's own
    words, since those are the ones most tightly coupled to "what
    wiki page should get updated this cycle".

    Drops very short signals (screenshot descriptions like "background
    activity") that carry no content. Strips wiki-link syntax,
    collapses whitespace, and caps total length.
    """
    from lighthouse.observations.types import is_outbound

    outbound_pieces: list[str] = []
    inbound_pieces: list[str] = []

    for d in signal_items:
        text = (d.get("text", "") or "").strip()
        if len(text) < 20:
            continue
        source = (d.get("source") or "").strip()
        sender = (d.get("sender") or "").strip()

        if is_outbound(d):
            # David's own words — tag with a literal marker so the
            # embedder pulls on "David said X" as strong intent signal.
            # Strip the [SENT] email prefix if present since we're
            # replacing it with a cleaner marker.
            body = text[6:].lstrip() if text.startswith("[SENT]") else text
            piece = f"David said: {body}"
        elif source in _INFORMATIVE_SENDER_SOURCES and sender and sender != "system":
            piece = f"from {sender}: {text}"
        else:
            piece = text

        piece = re.sub(r"\[\[([^\]]+)\]\]", r"\1", piece)
        piece = re.sub(r"\s+", " ", piece)

        if is_outbound(d):
            outbound_pieces.append(piece)
        else:
            inbound_pieces.append(piece)

    if not outbound_pieces and not inbound_pieces:
        return ""

    # Outbound goes first. When outbound exists, we also include it
    # twice — crude but effective upweighting: the embedder is
    # length-weighted, so repetition pulls the query vector toward
    # David's words. Inbound follows, contributing context without
    # dominating.
    parts = outbound_pieces + outbound_pieces + inbound_pieces if outbound_pieces else inbound_pieces
    combined = " / ".join(parts)
    return combined[:_MAX_QUERY_CHARS]


def _uri_to_path(uri: str) -> Path | None:
    """Convert `qmd://wiki/people/mike-alfred.md` -> absolute path."""
    m = re.match(r"^qmd://([^/]+)/(.+)$", uri)
    if not m:
        return None
    rel = m.group(2)
    candidate = WIKI_DIR / rel
    if candidate.exists():
        return candidate
    return None


def _collect_hits(results: list[tuple[str, float]]) -> list[tuple[str, str]]:
    """Turn vector hits into (category, slug) tuples, filtered and deduped.

    Applies three filters in order:

      1. URI must resolve to an existing file in `people/` or `projects/`
         (skips prompts, root meta-files, backups, missing files).
      2. Similarity score must be ≥ `_MIN_SCORE` — drops the long tail
         of noise matches the embedder returns for any query.
      3. First-seen wins — de-duplicates by (category, slug).

    Stops once `_MAX_PAGES` hits accumulate, so there's a hard cap on
    per-cycle prompt size.
    """
    hits: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for uri, score in results:
        if score < _MIN_SCORE:
            continue
        path = _uri_to_path(uri)
        if path is None:
            continue
        category = path.parent.name
        if category not in _ALLOWED_CATEGORIES:
            continue
        key = (category, path.stem)
        if key in seen:
            continue
        seen.add(key)
        hits.append(key)
        if len(hits) >= _MAX_PAGES:
            break
    return hits


def _retrieve_vector(query: str) -> list[tuple[str, str]]:
    """Run one vector search against the wiki and return filtered hits.

    Fails soft — any exception (qmd subprocess error, missing embeddings,
    malformed JSON) returns an empty list and the caller falls back on
    the always-included index.md plus the remaining-pages footer.
    """
    if not query:
        return []
    try:
        results = vsearch_files(
            query,
            collection="wiki",
            limit=_VSEARCH_CANDIDATES,
            exclude_basenames=_META_BASENAMES,
        )
    except Exception:
        log.debug("vsearch_files failed", exc_info=True)
        return []
    return _collect_hits(results)


def _retrieve_bm25(entity_tokens: list[str]) -> list[tuple[str, str]]:
    """Run a BM25 search per entity token and merge the hits.

    BM25 wants short, keyword-shaped queries — single entity tokens
    ("peffer", "bladeandrose", "Palo Alto") are its sweet spot. Each
    call is ~100ms, and we cap at 10 tokens per cycle, so total BM25
    overhead is ≤1s. Results are deterministic and zero-setup.

    Uses a lower score threshold than vector because BM25 scores are
    higher in absolute terms (0.80+ is common for direct term
    matches) — its noise floor isn't 0.58, it's around 0.70.
    """
    if not entity_tokens:
        return []

    hits: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    for token in entity_tokens:
        if len(hits) >= _MAX_PAGES:
            break
        try:
            results = search_files(
                token,
                collection="wiki",
                limit=_BM25_PER_TOKEN_LIMIT,
                exclude_basenames=_META_BASENAMES,
            )
        except Exception:
            log.debug("search_files failed for %r", token, exc_info=True)
            continue

        for uri, score in results:
            # BM25 noise floor — relax to 0.70 since BM25 scores higher
            # in absolute terms than cosine vector scores. Below this is
            # usually a weak partial-term match.
            if score < _BM25_MIN_SCORE:
                continue
            path = _uri_to_path(uri)
            if path is None:
                continue
            category = path.parent.name
            if category not in _ALLOWED_CATEGORIES:
                continue
            key = (category, path.stem)
            if key in seen:
                continue
            seen.add(key)
            hits.append(key)
            if len(hits) >= _MAX_PAGES:
                break

    return hits


def _read_page(category: str, slug: str) -> str | None:
    """Read a single wiki page's contents, or None if it doesn't exist."""
    path = WIKI_DIR / category / f"{slug}.md"
    if not path.exists():
        return None
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _format_pages(pages: list[tuple[str, str, str]]) -> str:
    """Format retrieved pages the same way `render_for_prompt` does so the
    downstream prompt stays structurally identical."""
    if not pages:
        return "(no pages retrieved for this signal batch)"
    chunks = []
    for category, slug, content in pages:
        title = slug.replace("-", " ").title()
        m = re.match(r"^#\s+(.+)$", content, re.MULTILINE)
        if m:
            title = m.group(1).strip()
        chunks.append(
            f"### {category}/{slug}  —  {title}\n{content.strip()}"
        )
    return "\n\n".join(chunks)


def _all_slugs() -> list[tuple[str, str]]:
    """Return every (category, slug) in the wiki for the 'also available' footer."""
    out: list[tuple[str, str]] = []
    for category in _ALLOWED_CATEGORIES:
        cat_dir = WIKI_DIR / category
        if not cat_dir.is_dir():
            continue
        for path in sorted(cat_dir.glob("*.md")):
            out.append((category, path.stem))
    return out


def build_analysis_context(signal_items: list[dict]) -> str:
    """Build a focused wiki context for one analysis cycle.

    Returns a string for the `{wiki_text}` slot of the analyze_write
    prompt. Contains:

      1. The wiki index — compact catalog of every slug (~2 KB).
      2. Pages retrieved by BM25 on extracted entity tokens.
      3. Pages retrieved by vector search on the signal batch.
      4. A footer listing pages that exist but weren't retrieved, so
         the model can still name them by slug in its output.

    Falls back to the full-wiki dump only if retrieval found nothing
    AND the wiki is effectively empty (fresh install).
    """
    # Stage 1: BM25 on entity tokens. Fast (~100ms × N tokens), catches
    # exact keyword matches. Runs first so its precise hits anchor the
    # result order before vector adds recall.
    entity_tokens = _extract_entity_tokens(signal_items)
    bm25_hits = _retrieve_bm25(entity_tokens)

    # Stage 2: Vector on the concatenated signal text. Slower (~3s),
    # catches semantic matches BM25 missed. Merged into `bm25_hits`
    # deduplicated so the same page doesn't appear twice.
    query = _build_query(signal_items)
    vector_hits = _retrieve_vector(query)

    # Merge: BM25 order first (precision), then vector results not
    # already covered (recall). Caps at `_MAX_PAGES`.
    seen: set[tuple[str, str]] = set()
    retrieved: list[tuple[str, str]] = []
    for key in bm25_hits + vector_hits:
        if key in seen:
            continue
        seen.add(key)
        retrieved.append(key)
        if len(retrieved) >= _MAX_PAGES:
            break

    all_pages = _all_slugs()

    # Read retrieved page contents from disk
    pages: list[tuple[str, str, str]] = []
    for category, slug in retrieved:
        content = _read_page(category, slug)
        if content is not None:
            pages.append((category, slug, content))

    # Only fall back to the full wiki if nothing retrieved AND no pages
    # exist at all (first few cycles on a fresh install).
    if not pages and not all_pages:
        log.info("wiki_retrieval: empty wiki — falling back to full render")
        return wiki_store.render_for_prompt()

    # Always include the index so the model sees the complete catalog.
    index_text = render_index_for_prompt().strip() or "(index not yet built)"

    retrieved_keys = {(c, s) for c, s, _ in pages}
    remaining = [f"{c}/{s}" for c, s in all_pages if (c, s) not in retrieved_keys]
    if len(remaining) > 40:
        remaining = remaining[:40] + [f"... (+{len(remaining) - 40} more)"]

    parts = [
        "## Wiki catalog (every page in the wiki)",
        index_text,
        "",
        "## Retrieved pages (relevant to the current signal batch)",
        _format_pages(pages),
    ]
    if remaining:
        parts.append("")
        parts.append(
            "## Other pages available on request\n"
            + ", ".join(remaining)
            + "\n\n(If any of these are relevant to the signals above, "
            "reference them by slug in your wiki_updates — they're "
            "in the catalog even if not retrieved in full.)"
        )

    log.info(
        "wiki_retrieval: %d bm25 + %d vector → %d merged, %d remaining",
        len(bm25_hits),
        len(vector_hits),
        len(pages),
        len(remaining),
    )
    return "\n".join(parts)
