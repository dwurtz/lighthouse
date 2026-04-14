"""Cross-page contradiction detection — runs alongside dedup 3x/day.

Dedup handles pages that describe the *same* entity. Contradictions live
between pages that describe *different* entities whose content happens to
reference the same subject (Amanda's page vs. the Blade & Rose project
page mentioning Amanda's new Google email). Those pages may never get
co-retrieved by the same integrate batch, so contradictions between them
drift silently.

The detection pass piggybacks on the same sqlite-vec + mean-pooled QMD
embeddings dedup already maintains. It walks the similarity matrix with
a **lower** threshold than dedup (dedup uses >=0.82, so any pair at or
above that has already been merged or rejected as hierarchy). Pairs in
the contradiction window (``CONTRA_MIN`` .. ``CONTRA_MAX``) are grouped
into small clusters (up to ``MAX_CLUSTER_SIZE`` pages each), truncated,
and handed to Flash-Lite which returns a structured list of contradiction
fixes. Each fix becomes a regular ``wiki.apply_updates`` entry with
``action="update"`` and a reason — no bypass, no private code path.

Hard caps keep this bounded to a fixed Flash-Lite cost per run:

    MAX_CLUSTERS_PER_RUN  = 50
    MAX_CLUSTER_SIZE      = 6   pages/cluster
    MAX_FIXES_PER_RUN     = 10  applied fixes/run

No fallbacks. Any failure raises loudly so the user sees the real
problem.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from deja import audit
from deja import wiki as wiki_store
from deja.config import QMD_COLLECTION, QMD_DB_PATH, WIKI_DIR
from deja.dedup import (
    _connect_qmd_db,
    _load_doc_vectors,
    _path_to_pageid,
    _parse_confirm_json,
    _FLASH_LITE_INPUT_PER_MTOK,
    _FLASH_LITE_OUTPUT_PER_MTOK,
)
from deja.llm_client import GeminiClient
from deja.prompts import load as load_prompt

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

# Similarity window for contradiction clustering. Dedup's threshold is
# 0.82, so anything at or above that should already be merged (or
# rejected as a hierarchy relationship). We look **below** that for
# pairs similar enough to talk about the same subject but not similar
# enough to be the same entity.
CONTRA_MIN = 0.65
CONTRA_MAX = 0.82

# Per-run caps. Each is a hard ceiling — not an average.
MAX_CLUSTERS_PER_RUN = 50
MAX_CLUSTER_SIZE = 6
MAX_FIXES_PER_RUN = 10

# Per-page body budget in the prompt. Full body is ideal for
# contradiction detection (truncation could hide the conflicting
# sentence), but we bound it so a cluster of 6 long pages doesn't
# blow the context budget.
PER_PAGE_BODY_CHARS = 2000

CONFIRM_MODEL = "gemini-2.5-flash"
# Upgraded from Flash-Lite 2026-04-13 after observing a batch of
# spurious "contradictions" that were really complementary mentions
# of the same entity (recruiter page "confirms" same role as self-page,
# not contradicts). Reflect runs 3x/day so the ~4x cost increase is
# pennies/day — worth the accuracy.

META_FILES = {"index.md", "log.md", "reflection.md", "goals.md"}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class ContraCluster:
    """A small group of topically-related pages to send to Flash-Lite."""

    page_ids: list[str]  # ["category/slug", ...]
    mean_similarity: float  # for ordering clusters most-promising-first


@dataclass
class ContradictionFix:
    """One confirmed contradiction fix ready to apply via wiki.apply_updates."""

    stale_page: str  # "category/slug"
    current_page: str  # "category/slug"
    stale_claim: str
    current_claim: str
    reason: str
    rewritten_content: str


@dataclass
class ContradictionSummary:
    """Per-run stats for logging + audit."""

    clusters_considered: int = 0
    clusters_reviewed: int = 0
    contradictions_returned: int = 0
    fixes_applied: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0

    def as_dict(self) -> dict:
        return {
            "clusters_considered": self.clusters_considered,
            "clusters_reviewed": self.clusters_reviewed,
            "contradictions_returned": self.contradictions_returned,
            "fixes_applied": self.fixes_applied,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": round(self.cost_usd, 6),
        }


# ---------------------------------------------------------------------------
# 1. Clustering — reuse the dedup vector pass, lower threshold
# ---------------------------------------------------------------------------


def _build_clusters(
    paths: list[str],
    mat: np.ndarray,
) -> list[ContraCluster]:
    """Group pages with pairwise similarity in the contradiction window.

    Uses a greedy connected-components walk over the adjacency graph where
    an edge exists iff ``CONTRA_MIN <= sim < CONTRA_MAX``. Clusters of
    size 1 are dropped (a page alone can't contradict itself). Clusters
    larger than MAX_CLUSTER_SIZE are split by repeatedly peeling off the
    MAX_CLUSTER_SIZE nodes with highest average similarity to the cluster
    mean — this keeps the densest sub-clusters intact instead of truncating
    arbitrarily.

    Returns clusters sorted by ``mean_similarity`` descending so the most
    promising ones get reviewed first when the run cap is tight.
    """
    n = len(paths)
    if n < 2:
        return []

    # Adjacency: for each i, neighbors j>i with sim in [CONTRA_MIN, CONTRA_MAX)
    sim = mat @ mat.T
    adj: dict[int, set[int]] = {i: set() for i in range(n)}
    for i in range(n):
        for j in range(i + 1, n):
            s = float(sim[i, j])
            if CONTRA_MIN <= s < CONTRA_MAX:
                adj[i].add(j)
                adj[j].add(i)

    # Connected components via BFS
    seen: set[int] = set()
    components: list[list[int]] = []
    for start in range(n):
        if start in seen or not adj[start]:
            continue
        stack = [start]
        comp: list[int] = []
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            comp.append(node)
            stack.extend(adj[node] - seen)
        if len(comp) >= 2:
            components.append(sorted(comp))

    # Split oversized components. We repeatedly pick the MAX_CLUSTER_SIZE
    # nodes with highest mean similarity to the component's centroid and
    # emit that as a cluster, then remove them and continue. This is
    # deliberately simple — the point is to bound prompt size, not find
    # the optimal partition.
    clusters: list[ContraCluster] = []
    for comp in components:
        remaining = list(comp)
        while remaining:
            if len(remaining) <= MAX_CLUSTER_SIZE:
                pick = remaining
                remaining = []
            else:
                sub_mat = mat[remaining]
                centroid = sub_mat.mean(axis=0)
                norm = np.linalg.norm(centroid)
                if norm > 0:
                    centroid = centroid / norm
                scores = sub_mat @ centroid
                order = np.argsort(-scores)
                pick_idx = order[:MAX_CLUSTER_SIZE]
                keep_idx = order[MAX_CLUSTER_SIZE:]
                pick = [remaining[int(k)] for k in pick_idx]
                remaining = [remaining[int(k)] for k in keep_idx]

            if len(pick) < 2:
                # A singleton left over after splitting — drop it.
                continue

            page_ids = [_path_to_pageid(paths[i]) for i in pick]
            # Mean pairwise similarity (upper triangle only) for cluster ranking
            pair_vals: list[float] = []
            for a_idx in range(len(pick)):
                for b_idx in range(a_idx + 1, len(pick)):
                    pair_vals.append(float(sim[pick[a_idx], pick[b_idx]]))
            mean_sim = sum(pair_vals) / len(pair_vals) if pair_vals else 0.0
            clusters.append(ContraCluster(page_ids=page_ids, mean_similarity=mean_sim))

    clusters.sort(key=lambda c: c.mean_similarity, reverse=True)
    return clusters


def find_contradiction_clusters() -> list[ContraCluster]:
    """Return the ranked list of clusters to review this run.

    Wraps dedup's sqlite-vec loader. Raises on any infra failure (missing
    db, empty collection, sqlite-vec not installed) — the same loud-fail
    posture dedup uses.
    """
    db = _connect_qmd_db()
    try:
        paths, mat = _load_doc_vectors(db)
    finally:
        db.close()
    return _build_clusters(paths, mat)


# ---------------------------------------------------------------------------
# 2. Prompt assembly
# ---------------------------------------------------------------------------


def _load_page_for_cluster(page_id: str) -> str:
    """Return the raw page body (truncated to PER_PAGE_BODY_CHARS)."""
    path = WIKI_DIR / f"{page_id}.md"
    if not path.exists():
        raise RuntimeError(
            f"Contradict: page {page_id} missing from disk at {path}. "
            f"QMD index is out of sync with the wiki."
        )
    raw = path.read_text(encoding="utf-8")
    if len(raw) > PER_PAGE_BODY_CHARS:
        raw = raw[:PER_PAGE_BODY_CHARS] + "\n\n…(truncated)"
    return raw


def _build_cluster_block(cluster: ContraCluster) -> str:
    """Format a cluster as a text block for the contradict prompt.

    Each page is framed with `=== <page_id> ===` delimiters so the LLM
    can unambiguously reference pages by id in its response. No further
    shaping — the LLM needs to see the real page text to spot real
    contradictions.
    """
    parts: list[str] = []
    for pid in cluster.page_ids:
        body = _load_page_for_cluster(pid)
        parts.append(f"=== {pid} ===\n{body}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# 3. Flash-Lite call
# ---------------------------------------------------------------------------


async def _call_flash_lite(prompt: str) -> tuple[dict, int, int]:
    """Call Flash-Lite with structured-JSON response. Retry once, then raise."""
    client = GeminiClient()
    config = {
        "response_mime_type": "application/json",
        "max_output_tokens": 32768,
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
            log.warning("Contradict: Flash-Lite attempt %d failed: %s", attempt, e)
            if attempt == 2:
                raise RuntimeError(
                    f"Contradict: Flash-Lite failed after 2 attempts: {e}"
                ) from e
    else:  # pragma: no cover
        raise RuntimeError(f"Contradict: Flash-Lite failed: {last_exc}")

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


# ---------------------------------------------------------------------------
# 4. Cluster review — orchestrate prompt + parse + collect fixes
# ---------------------------------------------------------------------------


def _fix_from_decision(d: dict, cluster_ids: set[str]) -> ContradictionFix | None:
    """Validate one contradiction decision and materialise a ContradictionFix.

    Returns None for malformed entries with a warning — a single bad
    decision should not torpedo the whole run (unlike dedup, where
    coverage is required). Contradiction detection is best-effort per
    cluster; the hard cap on applied fixes is what bounds cost.
    """
    stale_page = (d.get("stale_page") or "").strip()
    current_page = (d.get("current_page") or "").strip()
    stale_claim = (d.get("stale_claim") or "").strip()
    current_claim = (d.get("current_claim") or "").strip()
    reason = (d.get("reason") or "").strip()
    rewritten = (d.get("rewritten_stale_content") or "").strip()

    if not stale_page or not current_page:
        log.warning("Contradict: decision missing page ids: %r", d)
        return None
    if stale_page == current_page:
        log.warning("Contradict: decision has stale == current (%s)", stale_page)
        return None
    if stale_page not in cluster_ids:
        log.warning(
            "Contradict: decision references stale_page %r not in cluster %r",
            stale_page, cluster_ids,
        )
        return None
    if current_page not in cluster_ids:
        log.warning(
            "Contradict: decision references current_page %r not in cluster %r",
            current_page, cluster_ids,
        )
        return None
    if not rewritten:
        log.warning(
            "Contradict: decision for %s is missing rewritten_stale_content",
            stale_page,
        )
        return None
    if not reason:
        reason = f"contradicted by {current_page}"

    return ContradictionFix(
        stale_page=stale_page,
        current_page=current_page,
        stale_claim=stale_claim,
        current_claim=current_claim,
        reason=reason,
        rewritten_content=rewritten,
    )


async def review_clusters(
    clusters: list[ContraCluster],
) -> tuple[list[ContradictionFix], ContradictionSummary]:
    """Review up to MAX_CLUSTERS_PER_RUN clusters, collect contradiction fixes.

    Stops accumulating fixes once MAX_FIXES_PER_RUN is reached, but still
    counts tokens for whatever clusters were reviewed up to that point.
    """
    summary = ContradictionSummary()
    summary.clusters_considered = len(clusters)
    if not clusters:
        return [], summary

    prompt_template = load_prompt("contradict")
    if "{cluster}" not in prompt_template:
        raise RuntimeError(
            "Contradict prompt is missing the {cluster} placeholder. "
            "Check the bundled contradict.md in default_assets/prompts/."
        )

    to_review = clusters[:MAX_CLUSTERS_PER_RUN]
    log.info(
        "Contradict: reviewing %d/%d cluster(s) via %s "
        "(similarity window %.2f..%.2f, max %d pages/cluster)",
        len(to_review), len(clusters), CONFIRM_MODEL,
        CONTRA_MIN, CONTRA_MAX, MAX_CLUSTER_SIZE,
    )

    fixes: list[ContradictionFix] = []
    total_in = 0
    total_out = 0

    for idx, cluster in enumerate(to_review, start=1):
        if len(fixes) >= MAX_FIXES_PER_RUN:
            log.info(
                "Contradict: hit fix cap (%d) after %d/%d cluster(s) — "
                "stopping early.",
                MAX_FIXES_PER_RUN, idx - 1, len(to_review),
            )
            break

        try:
            cluster_block = _build_cluster_block(cluster)
        except RuntimeError as e:
            # Missing page on disk — skip this cluster, log, continue.
            log.warning("Contradict: skipping cluster %d: %s", idx, e)
            continue

        try:
            prompt = prompt_template.format(cluster=cluster_block)
        except (KeyError, IndexError) as e:
            raise RuntimeError(
                f"Contradict prompt template has an unexpected format "
                f"placeholder: {e}. Check the bundled contradict.md in default_assets/prompts/ — "
                f"only {{cluster}} should be an unescaped placeholder; "
                f"all literal braces must be doubled as {{{{ }}}}."
            ) from e

        log.info(
            "Contradict cluster %d/%d: %d page(s), %d prompt chars, sim=%.3f",
            idx, len(to_review), len(cluster.page_ids),
            len(prompt), cluster.mean_similarity,
        )

        parsed, in_tok, out_tok = await _call_flash_lite(prompt)
        total_in += in_tok
        total_out += out_tok
        summary.clusters_reviewed += 1

        decisions = parsed.get("contradictions") if isinstance(parsed, dict) else None
        if not isinstance(decisions, list):
            log.warning(
                "Contradict cluster %d: response has no 'contradictions' "
                "list. Got: %r", idx, parsed,
            )
            continue

        summary.contradictions_returned += len(decisions)
        cluster_ids = set(cluster.page_ids)
        for d in decisions:
            if not isinstance(d, dict):
                continue
            fix = _fix_from_decision(d, cluster_ids)
            if fix is None:
                continue
            fixes.append(fix)
            if len(fixes) >= MAX_FIXES_PER_RUN:
                break

    summary.input_tokens = total_in
    summary.output_tokens = total_out
    summary.cost_usd = (
        (total_in / 1_000_000) * _FLASH_LITE_INPUT_PER_MTOK
        + (total_out / 1_000_000) * _FLASH_LITE_OUTPUT_PER_MTOK
    )
    return fixes, summary


# ---------------------------------------------------------------------------
# 5. Apply fixes — funnel through wiki.apply_updates
# ---------------------------------------------------------------------------


def _split_page_id(page_id: str) -> tuple[str, str]:
    if "/" not in page_id:
        raise RuntimeError(
            f"Contradict apply: bad page id {page_id!r}, expected 'category/slug'"
        )
    category, slug = page_id.split("/", 1)
    if category not in ("people", "projects"):
        raise RuntimeError(
            f"Contradict apply: refuse to rewrite non-people/projects page "
            f"{page_id!r}"
        )
    return category, slug


def apply_fixes(fixes: list[ContradictionFix]) -> int:
    """Apply confirmed contradiction fixes through ``wiki.apply_updates``.

    Each fix becomes one wiki_update entry with ``action="update"``. The
    apply path handles frontmatter preservation, git commits, and the
    normal ``wiki_write`` audit row. We additionally record one
    ``contradiction_fix`` audit row per fix so the "why" is searchable
    by trigger kind.
    """
    if not fixes:
        return 0

    updates: list[dict] = []
    for fix in fixes:
        category, slug = _split_page_id(fix.stale_page)
        updates.append(
            {
                "category": category,
                "slug": slug,
                "action": "update",
                "content": fix.rewritten_content,
                "reason": (
                    f"removing stale claim — contradicted by {fix.current_page}"
                    f" ({fix.reason})"
                ),
            }
        )

    applied = wiki_store.apply_updates(updates)

    # One contradiction_fix audit row per fix we actually sent through —
    # wiki.apply_updates may drop entries whose category/slug failed
    # validation, but that's a shape bug we want surfaced, not silenced.
    for fix in fixes:
        try:
            audit.record(
                "contradiction_fix",
                target=fix.stale_page,
                reason=(
                    f"removing '{fix.stale_claim[:120]}' — contradicted by "
                    f"{fix.current_page}: {fix.reason}"
                ),
                trigger={"kind": "dedup", "detail": "contradiction sweep"},
            )
        except Exception:
            log.debug("Contradict: audit.record failed", exc_info=True)

    return applied


# ---------------------------------------------------------------------------
# 6. Top-level entrypoint — called from reflection_scheduler.run_reflection
# ---------------------------------------------------------------------------


async def run_contradiction_sweep() -> dict:
    """Run one full contradiction sweep. Called by reflection_scheduler
    after ``deja.dedup.run_dedup`` completes.

    Dedup runs first because its merges shrink the corpus — any page
    removed by dedup can't participate in a contradiction cluster, which
    is exactly right. Dedup also refreshes qmd at the end of its cycle,
    so by the time we run, the sqlite-vec index already reflects the
    post-merge corpus.

    Returns the ContradictionSummary as a dict. Raises on infra failure
    (missing qmd db, prompt file missing, Flash-Lite exhausted retries);
    logs and continues on per-cluster failures.
    """
    wiki_store.ensure_dirs()

    clusters = find_contradiction_clusters()
    log.info(
        "Contradict: %d cluster(s) found in similarity window %.2f..%.2f "
        "(cap %d per run)",
        len(clusters), CONTRA_MIN, CONTRA_MAX, MAX_CLUSTERS_PER_RUN,
    )

    if not clusters:
        return ContradictionSummary().as_dict()

    fixes, summary = await review_clusters(clusters)

    if fixes:
        applied = apply_fixes(fixes)
        summary.fixes_applied = applied

    log.info("Contradict complete: %s", summary.as_dict())
    return summary.as_dict()
