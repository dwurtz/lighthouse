"""Vector-based duplicate detection and merge pass.

Runs 3x/day on the Deja wiki via the reflection scheduler:

  1. Load QMD embeddings from ~/.cache/qmd/index.sqlite via sqlite-vec
  2. Compute pairwise cosine similarity for all people/projects pages,
     filter to candidates above the configured threshold (0.82)
  3. Send candidate pairs to gemini-2.5-flash-lite for same_entity judgment,
     with a hierarchy rejection rule to prevent part-of relationships from
     being merged
  4. Apply confirmed merges to the wiki: write the merged body to the
     canonical page, delete non-canonical pages, commit via wiki_git.

No fallbacks. Any failure raises loudly so the user sees the real problem.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from deja import wiki as wiki_store
from deja.config import WIKI_DIR
from deja.llm_client import GeminiClient
from deja.prompts import load as load_prompt

log = logging.getLogger(__name__)

SIMILARITY_THRESHOLD = 0.82
CONFIRM_MODEL = "gemini-2.5-flash-lite"
QMD_DB_PATH = Path.home() / ".cache" / "qmd" / "index.sqlite"
QMD_COLLECTION = "Deja"
BODY_SNIPPET_CHARS = 500
MAX_CANDIDATES_PER_RUN = 300  # sanity ceiling — well above typical run
META_FILES = {"index.md", "log.md", "reflection.md", "goals.md"}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class CandidatePair:
    """One pair of wiki pages above the vector similarity threshold."""

    page_a: str  # "category/slug" (no .md suffix)
    page_b: str
    similarity: float


@dataclass
class ConfirmedMerge:
    """A merge Flash-Lite confirmed. Apply directly — no review."""

    canonical: str  # "category/slug"
    duplicates: list[str]  # ["category/slug", ...]
    merged_content: str
    reason: str


@dataclass
class DedupSummary:
    """Per-run stats so callers and logs can see what happened."""

    candidates_found: int = 0
    decisions_returned: int = 0
    merges_confirmed: int = 0
    merges_applied: int = 0
    duplicates_deleted: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    candidates: list[CandidatePair] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "candidates_found": self.candidates_found,
            "decisions_returned": self.decisions_returned,
            "merges_confirmed": self.merges_confirmed,
            "merges_applied": self.merges_applied,
            "duplicates_deleted": self.duplicates_deleted,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": round(self.cost_usd, 6),
        }


# Flash-Lite pricing (as of 2026-04). Used to log per-run cost.
_FLASH_LITE_INPUT_PER_MTOK = 0.10
_FLASH_LITE_OUTPUT_PER_MTOK = 0.40


# ---------------------------------------------------------------------------
# 1. Candidate detection — sqlite-vec vector similarity
# ---------------------------------------------------------------------------


def _connect_qmd_db() -> sqlite3.Connection:
    if not QMD_DB_PATH.exists():
        raise RuntimeError(
            f"QMD index not found at {QMD_DB_PATH}. "
            f"Run `qmd embed` against the Deja collection before running dedup."
        )
    try:
        import sqlite_vec  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "sqlite-vec is required for dedup but is not installed. "
            "Add it to pyproject.toml and reinstall the venv."
        ) from e
    db = sqlite3.connect(QMD_DB_PATH)
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    return db


def _load_doc_vectors(db: sqlite3.Connection) -> tuple[list[str], np.ndarray]:
    """Return (paths, L2-normalized-mean-pooled-vectors) for all people/projects pages.

    Mean-pools multi-chunk documents so one row == one page. Raises if the
    QMD collection is empty or missing — a silent empty-return would hide
    a real indexing problem.
    """
    rows = db.execute(
        """
        SELECT d.path, cv.seq, vec_to_json(vv.embedding) AS vec_json
        FROM documents d
        JOIN content_vectors cv ON cv.hash = d.hash
        JOIN vectors_vec vv ON vv.hash_seq = d.hash || '_' || cv.seq
        WHERE d.collection = ?
          AND d.active = 1
          AND (d.path LIKE 'people/%' OR d.path LIKE 'projects/%')
        ORDER BY d.path, cv.seq
        """,
        (QMD_COLLECTION,),
    ).fetchall()

    if not rows:
        raise RuntimeError(
            f"QMD collection {QMD_COLLECTION!r} has no active people/projects "
            f"documents. Run `qmd update && qmd embed` on {WIKI_DIR} before dedup."
        )

    by_path: dict[str, list[list[float]]] = {}
    for path, _seq, vec_json in rows:
        name = Path(path).name
        if name in META_FILES:
            continue
        vec = json.loads(vec_json)
        by_path.setdefault(path, []).append(vec)

    paths = sorted(by_path)
    if not paths:
        raise RuntimeError(
            "All QMD rows for Deja were filtered out as meta files — "
            "nothing to dedup."
        )

    dim = len(by_path[paths[0]][0])
    mat = np.zeros((len(paths), dim), dtype=np.float32)
    for i, p in enumerate(paths):
        chunks = np.array(by_path[p], dtype=np.float32)
        pooled = chunks.mean(axis=0)
        norm = np.linalg.norm(pooled)
        if norm > 0:
            pooled = pooled / norm
        mat[i] = pooled
    return paths, mat


def _path_to_pageid(path: str) -> str:
    """Convert 'people/foo.md' to 'people/foo'."""
    return path.removesuffix(".md")


def find_candidates(threshold: float = SIMILARITY_THRESHOLD) -> list[CandidatePair]:
    """Return all people/projects page pairs with cosine similarity >= threshold.

    Raises on any failure (missing db, empty collection, sqlite-vec missing).
    The result is sorted by similarity descending.
    """
    db = _connect_qmd_db()
    try:
        paths, mat = _load_doc_vectors(db)
    finally:
        db.close()

    sim = mat @ mat.T
    pairs: list[CandidatePair] = []
    n = len(paths)
    for i in range(n):
        for j in range(i + 1, n):
            s = float(sim[i, j])
            if s >= threshold:
                pairs.append(
                    CandidatePair(
                        page_a=_path_to_pageid(paths[i]),
                        page_b=_path_to_pageid(paths[j]),
                        similarity=s,
                    )
                )
    pairs.sort(key=lambda p: p.similarity, reverse=True)
    return pairs


# ---------------------------------------------------------------------------
# 2. Confirmation — Flash-Lite same_entity judgment
# ---------------------------------------------------------------------------


_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)


def _load_page_for_prompt(page_id: str) -> tuple[str, str]:
    """Return (frontmatter_block, compact_body_snippet) for a 'category/slug' page."""
    path = WIKI_DIR / f"{page_id}.md"
    if not path.exists():
        raise RuntimeError(
            f"Dedup: candidate page {page_id} is missing from disk at {path}. "
            f"QMD index is out of sync with the wiki."
        )
    raw = path.read_text(encoding="utf-8")
    frontmatter = ""
    body = raw
    m = _FRONTMATTER_RE.match(raw)
    if m:
        frontmatter = m.group(1).strip()
        body = m.group(2).strip()
    body = re.sub(r"\s+", " ", body).strip()
    return frontmatter, body[:BODY_SNIPPET_CHARS]


def _build_pairs_block(candidates: list[CandidatePair]) -> str:
    lines: list[str] = []
    for i, c in enumerate(candidates, start=1):
        fm_a, body_a = _load_page_for_prompt(c.page_a)
        fm_b, body_b = _load_page_for_prompt(c.page_b)
        lines.append(
            f"### Pair {i}: {c.page_a} vs {c.page_b} (similarity: {c.similarity:.3f})"
        )
        lines.append(f"**{c.page_a}** frontmatter: {fm_a or '(none)'}")
        lines.append(f"**{c.page_a}** body: {body_a}")
        lines.append(f"**{c.page_b}** frontmatter: {fm_b or '(none)'}")
        lines.append(f"**{c.page_b}** body: {body_b}")
        lines.append("")
    return "\n".join(lines)


def _parse_confirm_json(raw: str) -> dict:
    """Parse the Flash-Lite response. Raises with raw payload on failure."""
    text = (raw or "").strip()
    if not text:
        raise RuntimeError("Dedup confirm: Flash-Lite returned empty response")
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    if "{" in text and "}" in text:
        start = text.index("{")
        end = text.rindex("}") + 1
        try:
            return json.loads(text[start:end])
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"Dedup confirm: unparseable Flash-Lite JSON. Error: {e}. "
                f"Raw payload (first 2000 chars): {raw[:2000]!r}"
            ) from e
    raise RuntimeError(
        f"Dedup confirm: Flash-Lite response has no JSON object. "
        f"Raw payload (first 2000 chars): {raw[:2000]!r}"
    )


async def _call_flash_lite(prompt: str) -> tuple[dict, int, int]:
    """Call Flash-Lite, retry once on exception, then raise.

    Returns (parsed_json, input_tokens, output_tokens_including_thoughts).
    """
    # 2.5 Flash-Lite supports up to 65K output tokens. We need headroom
    # because each confirmed merge includes a full rewritten page body
    # (aliases frontmatter + merged prose). 124 candidates with a handful
    # of merges can easily push past 16K.
    client = GeminiClient()
    config = {
        "response_mime_type": "application/json",
        "max_output_tokens": 65536,
        "temperature": 0.1,
    }
    last_exc: Exception | None = None
    for attempt in (1, 2):
        try:
            resp = await client._generate_full(
                model=CONFIRM_MODEL,
                contents=prompt,
                config_dict=config,
            )
            break
        except Exception as e:
            last_exc = e
            log.warning("Dedup confirm: Flash-Lite attempt %d failed: %s", attempt, e)
            if attempt == 2:
                raise RuntimeError(
                    f"Dedup confirm: Flash-Lite failed after 2 attempts: {e}"
                ) from e
    else:  # pragma: no cover — loop always breaks or raises
        raise RuntimeError(f"Dedup confirm: Flash-Lite failed: {last_exc}")

    if isinstance(resp, dict):
        raw_text = resp.get("text") or ""
        um = resp.get("usage_metadata") or {}
        in_tok = int(um.get("prompt_token_count") or 0)
        out_tok = int(um.get("candidates_token_count") or 0)
        thoughts = int(um.get("thoughts_token_count") or 0)
    else:
        raw_text = getattr(resp, "text", "") or ""
        um = getattr(resp, "usage_metadata", None)
        in_tok = int(getattr(um, "prompt_token_count", 0) or 0) if um else 0
        out_tok = int(getattr(um, "candidates_token_count", 0) or 0) if um else 0
        thoughts = int(getattr(um, "thoughts_token_count", 0) or 0) if um else 0

    parsed = _parse_confirm_json(raw_text)
    return parsed, in_tok, out_tok + thoughts


def _collect_merges(decisions: list[dict]) -> list[ConfirmedMerge]:
    """Turn Flash-Lite decisions into ConfirmedMerge objects.

    Decisions with same_entity=False are dropped. Unions pairs that share
    a canonical into a single merge (e.g. a -> X and b -> X collapse to
    one ConfirmedMerge with duplicates=[a,b]).
    """
    by_canonical: dict[str, ConfirmedMerge] = {}
    for d in decisions:
        if not d.get("same_entity"):
            continue
        canonical = (d.get("canonical") or "").strip()
        merged_content = (d.get("merged_content") or "").strip()
        reason = (d.get("reason") or "").strip()
        page_a = (d.get("page_a") or "").strip()
        page_b = (d.get("page_b") or "").strip()

        if not canonical or not merged_content:
            raise RuntimeError(
                f"Dedup confirm: same_entity=true decision missing canonical "
                f"or merged_content: {d!r}"
            )
        if canonical not in (page_a, page_b):
            raise RuntimeError(
                f"Dedup confirm: canonical {canonical!r} is neither page in "
                f"the pair ({page_a!r}, {page_b!r}): {d!r}"
            )

        dup = page_b if canonical == page_a else page_a
        existing = by_canonical.get(canonical)
        if existing is None:
            by_canonical[canonical] = ConfirmedMerge(
                canonical=canonical,
                duplicates=[dup],
                merged_content=merged_content,
                reason=reason,
            )
        else:
            if dup not in existing.duplicates:
                existing.duplicates.append(dup)
            # Keep the most recent merged_content — the model should be
            # consistent, but later cluster members see more context.
            existing.merged_content = merged_content
            if reason and reason not in existing.reason:
                existing.reason = f"{existing.reason}; {reason}" if existing.reason else reason
    return list(by_canonical.values())


CONFIRM_BATCH_SIZE = 20  # pairs per Flash-Lite call. Smaller batches get
                          # full coverage reliably because the response
                          # stays short enough for the model to enumerate
                          # every pair instead of silently dropping obvious
                          # rejects. Empirically: 40 pairs/batch failed
                          # coverage on ~5/35 pairs; 20 pairs is the
                          # stable sweet spot.


async def confirm_candidates(
    candidates: list[CandidatePair],
) -> tuple[list[ConfirmedMerge], DedupSummary]:
    """Ask Flash-Lite to judge each candidate pair.

    Batches the candidates into chunks of CONFIRM_BATCH_SIZE to keep each
    response small enough that Flash-Lite reliably returns a decision for
    every pair (large batches trigger a silent-skip failure mode). Raises
    on any failure — callers get a loud error, not a silent miss.
    """
    summary = DedupSummary()
    summary.candidates_found = len(candidates)
    summary.candidates = list(candidates)
    if not candidates:
        return [], summary
    if len(candidates) > MAX_CANDIDATES_PER_RUN:
        raise RuntimeError(
            f"Dedup: {len(candidates)} candidate pairs exceeds cap "
            f"{MAX_CANDIDATES_PER_RUN}. Raise the threshold or investigate "
            f"why the wiki exploded with near-duplicates."
        )

    prompt_template = load_prompt("dedup_confirm")
    if "{pairs}" not in prompt_template:
        raise RuntimeError(
            "Dedup confirm prompt is missing the {pairs} placeholder. "
            "Check ~/Deja/prompts/dedup_confirm.md."
        )

    # Split into batches of CONFIRM_BATCH_SIZE
    batches: list[list[CandidatePair]] = [
        candidates[i : i + CONFIRM_BATCH_SIZE]
        for i in range(0, len(candidates), CONFIRM_BATCH_SIZE)
    ]
    log.info(
        "Dedup: confirming %d pair(s) via %s across %d batch(es) of ≤%d",
        len(candidates), CONFIRM_MODEL, len(batches), CONFIRM_BATCH_SIZE,
    )

    all_decisions: list[dict] = []
    total_in_tok = 0
    total_out_tok = 0
    for batch_idx, batch in enumerate(batches, start=1):
        try:
            prompt = prompt_template.format(pairs=_build_pairs_block(batch))
        except (KeyError, IndexError) as e:
            raise RuntimeError(
                f"Dedup confirm prompt template has an unexpected format "
                f"placeholder: {e}. Check ~/Deja/prompts/dedup_confirm.md — "
                f"only {{pairs}} should be an unescaped placeholder; all "
                f"literal braces must be doubled as {{{{ }}}}."
            ) from e

        log.info(
            "Dedup batch %d/%d: %d pair(s), %d prompt chars",
            batch_idx, len(batches), len(batch), len(prompt),
        )

        parsed, in_tok, out_tok = await _call_flash_lite(prompt)
        total_in_tok += in_tok
        total_out_tok += out_tok

        decisions = parsed.get("decisions") if isinstance(parsed, dict) else None
        if not isinstance(decisions, list):
            raise RuntimeError(
                f"Dedup confirm batch {batch_idx}: response JSON has no "
                f"'decisions' list. Got: {parsed!r}"
            )

        # Per-batch coverage check. If this batch misses pairs, we can't
        # meaningfully recover — raise with the batch index so the user
        # can investigate.
        covered: set[tuple[str, str]] = set()
        for d in decisions:
            if not isinstance(d, dict):
                continue
            a, b = d.get("page_a"), d.get("page_b")
            if isinstance(a, str) and isinstance(b, str):
                covered.add(tuple(sorted([a, b])))
        expected = {tuple(sorted([c.page_a, c.page_b])) for c in batch}
        missing = expected - covered
        if missing:
            sample = sorted(missing)[:10]
            raise RuntimeError(
                f"Dedup confirm batch {batch_idx}/{len(batches)}: "
                f"Flash-Lite omitted {len(missing)} of {len(expected)} "
                f"pairs. First missing: {sample}. Reduce CONFIRM_BATCH_SIZE "
                f"or switch to 2.5 Flash."
            )

        all_decisions.extend(decisions)

    summary.decisions_returned = len(all_decisions)
    summary.input_tokens = total_in_tok
    summary.output_tokens = total_out_tok
    summary.cost_usd = (
        (total_in_tok / 1_000_000) * _FLASH_LITE_INPUT_PER_MTOK
        + (total_out_tok / 1_000_000) * _FLASH_LITE_OUTPUT_PER_MTOK
    )

    merges = _collect_merges(all_decisions)
    decisions = all_decisions  # rebind for the log line below
    summary.merges_confirmed = len(merges)
    log.info(
        "Dedup: %d decision(s), %d confirmed merge(s), cost $%.4f",
        len(decisions), len(merges), summary.cost_usd,
    )
    return merges, summary


# ---------------------------------------------------------------------------
# 3. Apply merges — write canonical, delete duplicates, commit
# ---------------------------------------------------------------------------


def _split_page_id(page_id: str) -> tuple[str, str]:
    if "/" not in page_id:
        raise RuntimeError(f"Dedup apply: bad page id {page_id!r}, expected 'category/slug'")
    category, slug = page_id.split("/", 1)
    if category not in ("people", "projects"):
        raise RuntimeError(
            f"Dedup apply: refuse to merge non-people/projects page {page_id!r}"
        )
    return category, slug


def apply_merges(merges: list[ConfirmedMerge]) -> tuple[int, int]:
    """Apply confirmed merges to the wiki. Returns (merges_applied, duplicates_deleted).

    For each merge:
      1. Verify all involved pages exist on disk
      2. Write merged_content to the canonical page
      3. Delete every duplicate page
      4. Commit via wiki_git with a descriptive message

    Raises on any failure — partial application is never allowed.
    """
    if not merges:
        return 0, 0

    from deja.wiki_git import ensure_repo, commit_changes

    ensure_repo()

    # Pre-validate everything so we don't half-apply.
    for merge in merges:
        _split_page_id(merge.canonical)
        canon_path = WIKI_DIR / f"{merge.canonical}.md"
        if not canon_path.exists():
            raise RuntimeError(
                f"Dedup apply: canonical page {merge.canonical} does not exist "
                f"at {canon_path}. Aborting run — refusing to partially apply."
            )
        if not merge.duplicates:
            raise RuntimeError(
                f"Dedup apply: merge for {merge.canonical} has no duplicates. "
                f"Bad decision payload."
            )
        for dup in merge.duplicates:
            _split_page_id(dup)
            dup_path = WIKI_DIR / f"{dup}.md"
            if not dup_path.exists():
                raise RuntimeError(
                    f"Dedup apply: duplicate page {dup} does not exist at "
                    f"{dup_path}. Aborting run — refusing to partially apply."
                )
            if dup == merge.canonical:
                raise RuntimeError(
                    f"Dedup apply: merge has canonical == duplicate ({dup}). "
                    f"Bad decision payload."
                )

    merges_applied = 0
    duplicates_deleted = 0
    commit_lines: list[str] = []

    for merge in merges:
        cat, slug = _split_page_id(merge.canonical)
        try:
            wiki_store.write_page(cat, slug, merge.merged_content)
        except Exception as e:
            raise RuntimeError(
                f"Dedup apply: failed to write canonical {merge.canonical}: {e}. "
                f"Aborting — manual inspection required."
            ) from e

        deleted_slugs: list[str] = []
        for dup in merge.duplicates:
            dcat, dslug = _split_page_id(dup)
            removed = wiki_store.delete_page(dcat, dslug)
            if not removed:
                raise RuntimeError(
                    f"Dedup apply: delete_page returned False for {dup}. "
                    f"State is now inconsistent — canonical {merge.canonical} "
                    f"was updated but duplicate could not be removed."
                )
            deleted_slugs.append(dup)
            duplicates_deleted += 1

        merges_applied += 1
        commit_lines.append(
            f"dedup: merged {', '.join(deleted_slugs)} into {merge.canonical}"
        )
        log.info(
            "Dedup applied: %s <- %s (%s)",
            merge.canonical, deleted_slugs, (merge.reason or "")[:120],
        )

    # Single commit covering the whole cycle.
    if commit_lines:
        msg = commit_lines[0] if len(commit_lines) == 1 else (
            "dedup: applied {n} merges\n\n".format(n=len(commit_lines))
            + "\n".join(f"- {line.removeprefix('dedup: ')}" for line in commit_lines)
        )
        committed = commit_changes(msg)
        if not committed:
            raise RuntimeError(
                "Dedup apply: wiki_git.commit_changes returned False despite "
                "having written merges. Check wiki git state manually."
            )

    return merges_applied, duplicates_deleted


# ---------------------------------------------------------------------------
# 4. Top-level entrypoint — called by reflection_scheduler
# ---------------------------------------------------------------------------


async def run_dedup() -> dict:
    """Run one full dedup cycle. Called by reflection_scheduler.run_reflection.

    Steps:
      1. Refresh QMD index + embeddings so the vector pass sees current state
      2. find_candidates — pairwise cosine from sqlite-vec
      3. confirm_candidates (async) — Flash-Lite same_entity judgment
      4. apply_merges — write canonical, delete duplicates, commit
      5. Rebuild wiki index.md

    Returns the DedupSummary as a dict. Raises loudly on any failure.
    """
    import subprocess

    wiki_store.ensure_dirs()

    # Refresh QMD so the vector similarity pass sees the current wiki.
    # Failure here is not fatal — we still want dedup to run against the
    # last known embeddings rather than skipping — but we log it loudly.
    try:
        subprocess.run(["qmd", "update"], capture_output=True, timeout=60, check=False)
        subprocess.run(["qmd", "embed"], capture_output=True, timeout=300, check=False)
    except Exception:
        log.exception("Dedup: pre-run qmd refresh failed — proceeding with stale index")

    candidates = find_candidates(SIMILARITY_THRESHOLD)
    log.info("Dedup: %d candidate pair(s) at threshold %.2f", len(candidates), SIMILARITY_THRESHOLD)

    if not candidates:
        summary = DedupSummary()
        return summary.as_dict()

    merges, summary = await confirm_candidates(candidates)

    if merges:
        applied, deleted = apply_merges(merges)
        summary.merges_applied = applied
        summary.duplicates_deleted = deleted

        # Refresh index + qmd so the next cycle sees the merged state.
        try:
            from deja.wiki_catalog import rebuild_index
            rebuild_index()
        except Exception:
            log.exception("Dedup: rebuild_index failed after merges")
        try:
            subprocess.run(["qmd", "update"], capture_output=True, timeout=60, check=False)
            subprocess.run(["qmd", "embed"], capture_output=True, timeout=300, check=False)
        except Exception:
            log.exception("Dedup: post-run qmd refresh failed")
        try:
            from deja.llm.search import refresh_index
            refresh_index()
        except Exception:
            log.debug("Dedup: llm.search.refresh_index failed", exc_info=True)
        try:
            from deja import audit
            audit.record(
                "dedup_merge",
                target="wiki/*",
                reason=(
                    f"merged {summary.duplicates_deleted} duplicate page(s) into "
                    f"{summary.merges_applied} canonical page(s) "
                    f"(${summary.cost_usd:.4f})"
                ),
                trigger={"kind": "dedup", "detail": "scheduled pass"},
            )
        except Exception:
            log.debug("Dedup: audit.record failed", exc_info=True)

    log.info("Dedup complete: %s", summary.as_dict())
    return summary.as_dict()
