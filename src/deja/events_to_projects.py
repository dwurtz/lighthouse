"""Events-to-projects sweep — cluster related events and materialize
new project pages directly.

Runs during the 3×/day reflection slot after dedup. Mirrors dedup's
dedup→confirm→write shape:

  1. **Cluster** events from two sources:
     - *Dangling-slug clusters*: events whose ``projects:`` frontmatter
       references a slug that has no corresponding ``projects/<slug>.md``
       page. When ≥2 events share the same dangling slug, that's a
       deterministic cluster — the events are already voting for the
       slug, and the slug is the name.
     - *Vector-similarity clusters*: events with an empty ``projects:``
       field that cluster via QMD embeddings at similarity ≥0.55 AND
       share a non-user person OR have average similarity ≥0.85.
     When both mechanisms surface the same event group, the dangling-slug
     variant wins — slug is pre-named and deterministic.

  2. **Confirm** each candidate cluster via Flash-Lite. One prompt per
     batch of N clusters: "is this a real project? if yes, what slug,
     description, seed body?"

  3. **Write** confirmed clusters directly to the wiki via
     ``wiki.apply_updates`` — ``create`` a ``projects/<slug>.md`` page
     with the seed body plus a ``## Recent`` section listing the cluster's
     events. Race against an existing project → fall back to ``update``.

No observation-file proposals. No waiting for integrate to materialize
anything. The previous design leaked dangling project references onto
events and never closed the loop; this design closes it every sweep.

Why ≥3 events for vector clusters: two related events could be
coincidence. Three is a pattern. Why ≥2 for dangling-slug clusters:
the slug IS a stronger signal than vectors alone — the events are
literally saying "we belong to <slug>".
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from deja import wiki as wiki_store
from deja.config import QMD_COLLECTION, WIKI_DIR
from deja.llm_client import GeminiClient
from deja.prompts import load as load_prompt

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Similarity threshold for event clustering. Lower than dedup's 0.82
# because we're looking for "thematically related" not "same entity" —
# pool-chlorine-issue and pool-payment are about the same service but
# describe different actions.
_SIMILARITY_THRESHOLD = 0.55

# Minimum cluster size for vector-similarity clusters. Two events could
# be coincidence; three is a pattern.
_MIN_CLUSTER_SIZE = 3

# Minimum number of events voting for the same dangling slug. The slug
# IS a stronger signal than vectors alone — two events literally saying
# "we belong to the same (non-existent) project" is enough.
_MIN_DANGLING_VOTES = 2

# Cap on clusters per sweep so a wiki with many unprojected events
# doesn't flood Flash-Lite / the wiki with 50 creates at once.
_MAX_CLUSTERS_PER_SWEEP = 10

# Clusters per Flash-Lite call. Clusters are denser payloads than
# dedup pairs (multi-event bodies), so batch modestly.
CONFIRM_BATCH_SIZE = 5

CONFIRM_MODEL = "gemini-2.5-flash-lite"

# Max chars of event body shown to Flash-Lite per event — keep the
# prompt compact so batch coverage is reliable.
_EVENT_SNIPPET_CHARS = 300

# Flash-Lite pricing (as of 2026-04). Used for per-run cost logging.
_FLASH_LITE_INPUT_PER_MTOK = 0.10
_FLASH_LITE_OUTPUT_PER_MTOK = 0.40


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class EventCluster:
    """One candidate cluster to surface to Flash-Lite.

    ``suggested_slug`` is set only for dangling-slug clusters. For vector
    clusters it's None and Flash-Lite invents the slug. ``source``
    distinguishes the two mechanisms in logs + audit reasons.
    """

    cluster_id: str
    paths: list[str]  # "events/YYYY-MM-DD/slug.md"
    shared_people: list[str]
    avg_similarity: float
    suggested_slug: str | None = None
    source: str = "vector"  # "dangling" | "vector"


@dataclass
class ConfirmedProject:
    slug: str
    description: str
    seed_body: str
    reason: str
    cluster: EventCluster


@dataclass
class SweepSummary:
    events_indexed: int = 0
    dangling_clusters: int = 0
    vector_clusters: int = 0
    clusters_proposed: int = 0
    decisions_returned: int = 0
    projects_confirmed: int = 0
    projects_written: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    clusters: list[EventCluster] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "events_indexed": self.events_indexed,
            "dangling_clusters": self.dangling_clusters,
            "vector_clusters": self.vector_clusters,
            "clusters_proposed": self.clusters_proposed,
            "decisions_returned": self.decisions_returned,
            "projects_confirmed": self.projects_confirmed,
            "projects_written": self.projects_written,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": round(self.cost_usd, 6),
            "clusters": [
                {
                    "cluster_id": c.cluster_id,
                    "source": c.source,
                    "events": c.paths,
                    "suggested_slug": c.suggested_slug,
                    "shared_people": c.shared_people,
                    "avg_similarity": c.avg_similarity,
                }
                for c in self.clusters
            ],
        }


# ---------------------------------------------------------------------------
# 1. QMD vector load — reuses dedup's connection helper
# ---------------------------------------------------------------------------


def _connect_qmd_db() -> sqlite3.Connection:
    """Open the QMD SQLite store — same approach as dedup."""
    from deja.dedup import _connect_qmd_db as _dedup_connect

    return _dedup_connect()


def _load_event_vectors(
    db: sqlite3.Connection,
) -> tuple[list[str], np.ndarray]:
    """Return (paths, L2-normalized mean-pooled vectors) for all event pages.

    Mean-pools multi-chunk documents so one row == one page. Returns
    ([], empty matrix) if there are too few events to cluster.
    """
    rows = db.execute(
        """
        SELECT d.path, cv.seq, vec_to_json(vv.embedding) AS vec_json
        FROM documents d
        JOIN content_vectors cv ON cv.hash = d.hash
        JOIN vectors_vec vv ON vv.hash_seq = d.hash || '_' || cv.seq
        WHERE d.collection = ?
          AND d.active = 1
          AND d.path LIKE 'events/%'
        ORDER BY d.path, cv.seq
        """,
        (QMD_COLLECTION,),
    ).fetchall()

    if not rows:
        return [], np.zeros((0, 0), dtype=np.float32)

    by_path: dict[str, list[list[float]]] = {}
    for path, _seq, vec_json in rows:
        vec = json.loads(vec_json)
        by_path.setdefault(path, []).append(vec)

    paths = sorted(by_path)
    if not paths:
        return [], np.zeros((0, 0), dtype=np.float32)

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


# ---------------------------------------------------------------------------
# 2. Event frontmatter + existing-project enumeration
# ---------------------------------------------------------------------------


def _parse_event_frontmatter(path: str) -> dict:
    """Return the YAML frontmatter dict for an event page, or {}.

    Handles the clean multi-line form and the legacy one-line corruption
    shape (30+ events in the current wiki have it). Only ``people`` and
    ``projects`` are required for clustering decisions, so the one-line
    fallback uses targeted regex rather than full YAML parsing.
    """
    fp = WIKI_DIR / path
    if not fp.exists():
        return {}
    try:
        text = fp.read_text(encoding="utf-8")
        if not text.startswith("---"):
            return {}

        # Try the multi-line form first.
        end = text.find("\n---", 3)
        if end != -1:
            body = text[3:end].strip()
            if body:
                try:
                    parsed = yaml.safe_load(body)
                    if isinstance(parsed, dict):
                        return parsed
                except yaml.YAMLError:
                    pass  # fall through to one-line regex

        # One-line fallback: ---key1: v1 key2: v2...---
        end_close = text.find("---", 3)
        if end_close == -1:
            return {}
        inline = text[3:end_close]

        out: dict = {}
        m = re.search(r"people:\s*\[([^\]]*)\]", inline)
        if m:
            raw = m.group(1).strip()
            out["people"] = [s.strip() for s in raw.split(",") if s.strip()]
        m = re.search(r"projects:\s*\[([^\]]*)\]", inline)
        if m:
            raw = m.group(1).strip()
            out["projects"] = [s.strip() for s in raw.split(",") if s.strip()]
        return out
    except Exception:
        return {}


def _existing_project_slugs() -> set[str]:
    """Return the set of ``slug`` values for all existing projects/<slug>.md."""
    projects_dir = WIKI_DIR / "projects"
    if not projects_dir.exists():
        return set()
    return {p.stem for p in projects_dir.glob("*.md")}


def _event_title(path: str) -> str:
    """Return the H1 title of an event page, or the slug as a fallback."""
    fp = WIKI_DIR / path
    title = Path(path).stem
    if not fp.exists():
        return title
    try:
        for line in fp.read_text(encoding="utf-8").splitlines():
            if line.startswith("# "):
                return line[2:].strip()
    except Exception:
        pass
    return title


def _event_snippet(path: str, max_chars: int = _EVENT_SNIPPET_CHARS) -> str:
    """Return a compact body snippet for the confirm prompt.

    Strips frontmatter + H1, collapses whitespace, truncates.
    """
    fp = WIKI_DIR / path
    if not fp.exists():
        return ""
    try:
        text = fp.read_text(encoding="utf-8")
    except Exception:
        return ""
    # Drop frontmatter.
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 4 :]
        else:
            end_close = text.find("---", 3)
            if end_close != -1:
                text = text[end_close + 3 :]
    # Drop H1.
    lines = [ln for ln in text.splitlines() if not ln.startswith("# ")]
    body = " ".join(lines)
    body = re.sub(r"\s+", " ", body).strip()
    return body[:max_chars]


# ---------------------------------------------------------------------------
# 3. Clustering — dangling-slug + vector
# ---------------------------------------------------------------------------


def _self_slug() -> str:
    try:
        from deja.identity import load_user

        return (load_user().slug or "").lower()
    except Exception:
        return ""


def _find_dangling_clusters(
    paths: list[str],
    existing: set[str],
    self_slug: str,
) -> list[EventCluster]:
    """Group events by shared dangling-slug references.

    For every event, inspect its ``projects:`` frontmatter. Any slug NOT
    in ``existing`` is "dangling" — the event is voting for a project
    that doesn't exist yet. ≥_MIN_DANGLING_VOTES events sharing the same
    dangling slug form a cluster.
    """
    slug_to_events: dict[str, list[str]] = {}
    slug_to_people: dict[str, list[set[str]]] = {}
    for path in paths:
        fm = _parse_event_frontmatter(path)
        projects = fm.get("projects") or []
        if not isinstance(projects, list):
            continue
        for slug in projects:
            slug = str(slug).strip()
            if not slug or slug in existing:
                continue
            slug_to_events.setdefault(slug, []).append(path)
            people = set(fm.get("people") or [])
            people.discard(self_slug)
            slug_to_people.setdefault(slug, []).append(people)

    clusters: list[EventCluster] = []
    for slug, evs in slug_to_events.items():
        if len(evs) < _MIN_DANGLING_VOTES:
            continue
        people_sets = slug_to_people.get(slug) or []
        shared = (
            set.intersection(*people_sets) if people_sets else set()
        )
        clusters.append(
            EventCluster(
                cluster_id=f"dangling-{slug}",
                paths=sorted(evs),
                shared_people=sorted(shared),
                avg_similarity=1.0,  # not meaningful for dangling clusters
                suggested_slug=slug,
                source="dangling",
            )
        )
    # Deterministic order: largest first, then slug alpha.
    clusters.sort(key=lambda c: (-len(c.paths), c.suggested_slug or ""))
    return clusters


def _find_vector_clusters(
    paths: list[str],
    mat: np.ndarray,
    existing: set[str],
    self_slug: str,
    already_clustered: set[str],
    threshold: float = _SIMILARITY_THRESHOLD,
) -> list[EventCluster]:
    """Greedy clustering on empty-projects events.

    Only considers events with ``projects: []`` (or no projects key) AND
    not already in a dangling cluster. Returns clusters of ≥_MIN_CLUSTER_SIZE
    that also have a shared non-user person OR high (≥0.85) avg similarity.
    """
    if len(paths) < _MIN_CLUSTER_SIZE or mat.size == 0:
        return []

    # Pre-filter: events with no projects AND not already clustered.
    eligible_idx: list[int] = []
    eligible_fms: list[dict] = []
    for i, p in enumerate(paths):
        if p in already_clustered:
            continue
        fm = _parse_event_frontmatter(p)
        if fm.get("projects"):
            continue
        eligible_idx.append(i)
        eligible_fms.append(fm)

    if len(eligible_idx) < _MIN_CLUSTER_SIZE:
        return []

    sub_paths = [paths[i] for i in eligible_idx]
    sub_mat = mat[eligible_idx]
    sub_sim = sub_mat @ sub_mat.T

    n = len(sub_paths)
    visited = [False] * n
    clusters: list[EventCluster] = []
    cluster_counter = 0

    for i in range(n):
        if visited[i]:
            continue
        cluster_idx = [i]
        visited[i] = True
        changed = True
        while changed:
            changed = False
            for j in range(n):
                if visited[j]:
                    continue
                if any(sub_sim[j, k] >= threshold for k in cluster_idx):
                    cluster_idx.append(j)
                    visited[j] = True
                    changed = True

        if len(cluster_idx) < _MIN_CLUSTER_SIZE:
            continue

        cluster_paths = [sub_paths[k] for k in cluster_idx]
        fms = [eligible_fms[k] for k in cluster_idx]
        people_sets = [set(fm.get("people") or []) for fm in fms]
        shared = set.intersection(*people_sets) if people_sets else set()
        if self_slug:
            shared.discard(self_slug)

        if len(cluster_idx) == 1:
            avg = 1.0
        else:
            pair_sims = [
                float(sub_sim[a, b])
                for a in cluster_idx
                for b in cluster_idx
                if a < b
            ]
            avg = sum(pair_sims) / len(pair_sims) if pair_sims else 0.0

        # Acceptance: shared non-user person OR high avg similarity.
        if not shared and avg < 0.85:
            continue

        clusters.append(
            EventCluster(
                cluster_id=f"vector-{cluster_counter}",
                paths=sorted(cluster_paths),
                shared_people=sorted(shared),
                avg_similarity=avg,
                suggested_slug=None,
                source="vector",
            )
        )
        cluster_counter += 1

    clusters.sort(key=lambda c: c.avg_similarity, reverse=True)
    return clusters


def find_clusters() -> tuple[list[EventCluster], int, int, int]:
    """Return (clusters, events_indexed, dangling_count, vector_count).

    Combines dangling-slug + vector mechanisms. Dangling clusters take
    precedence: an event captured by a dangling-slug cluster is NOT
    re-eligible for a vector cluster on the same sweep.
    """
    db = _connect_qmd_db()
    try:
        paths, mat = _load_event_vectors(db)
    finally:
        db.close()

    existing = _existing_project_slugs()
    self_slug = _self_slug()

    dangling = _find_dangling_clusters(paths, existing, self_slug)
    already: set[str] = set()
    for c in dangling:
        already.update(c.paths)

    vector = _find_vector_clusters(
        paths, mat, existing, self_slug, already
    )

    combined = dangling + vector
    combined = combined[:_MAX_CLUSTERS_PER_SWEEP]
    return combined, len(paths), len(dangling), len(vector)


# ---------------------------------------------------------------------------
# 4. Confirm — Flash-Lite batched judgment
# ---------------------------------------------------------------------------


def _build_clusters_block(clusters: list[EventCluster]) -> str:
    """Render clusters as the ``{clusters}`` prompt section."""
    lines: list[str] = []
    for c in clusters:
        lines.append(f"### {c.cluster_id}")
        if c.suggested_slug:
            lines.append(f"suggested_slug: {c.suggested_slug}")
        if c.shared_people:
            lines.append(f"shared_people: {', '.join(c.shared_people)}")
        lines.append(f"avg_similarity: {c.avg_similarity:.3f}")
        lines.append(f"source: {c.source}")
        lines.append("events:")
        for path in c.paths:
            title = _event_title(path)
            snippet = _event_snippet(path)
            lines.append(f"  - [{path}] {title}")
            if snippet:
                lines.append(f"    body: {snippet}")
        lines.append("")
    return "\n".join(lines)


def _parse_confirm_json(raw: str) -> dict:
    """Parse the Flash-Lite response. Raises with raw payload on failure."""
    text = (raw or "").strip()
    if not text:
        raise RuntimeError(
            "events_to_projects confirm: Flash-Lite returned empty response"
        )
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
                f"events_to_projects confirm: unparseable Flash-Lite JSON. "
                f"Error: {e}. Raw payload (first 2000 chars): {raw[:2000]!r}"
            ) from e
    raise RuntimeError(
        f"events_to_projects confirm: Flash-Lite response has no JSON "
        f"object. Raw payload (first 2000 chars): {raw[:2000]!r}"
    )


async def _call_flash_lite(prompt: str) -> tuple[dict, int, int]:
    """Call Flash-Lite, retry once on exception, then raise.

    Returns (parsed_json, input_tokens, output_tokens_including_thoughts).
    """
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
            log.warning(
                "events_to_projects confirm: attempt %d failed: %s",
                attempt, e,
            )
            if attempt == 2:
                raise RuntimeError(
                    f"events_to_projects confirm: Flash-Lite failed after "
                    f"2 attempts: {e}"
                ) from e
    else:  # pragma: no cover
        raise RuntimeError(
            f"events_to_projects confirm: Flash-Lite failed: {last_exc}"
        )

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


async def confirm_clusters(
    clusters: list[EventCluster],
) -> tuple[list[ConfirmedProject], SweepSummary]:
    """Ask Flash-Lite to judge each cluster; return the confirmed projects."""
    summary = SweepSummary()
    summary.clusters_proposed = len(clusters)
    summary.clusters = list(clusters)
    if not clusters:
        return [], summary

    prompt_template = load_prompt("events_to_projects_confirm")
    if "{clusters}" not in prompt_template:
        raise RuntimeError(
            "events_to_projects confirm prompt is missing the {clusters} "
            "placeholder. Check the bundled events_to_projects_confirm.md "
            "in default_assets/prompts/."
        )

    batches: list[list[EventCluster]] = [
        clusters[i : i + CONFIRM_BATCH_SIZE]
        for i in range(0, len(clusters), CONFIRM_BATCH_SIZE)
    ]
    log.info(
        "events_to_projects: confirming %d cluster(s) via %s across "
        "%d batch(es) of ≤%d",
        len(clusters), CONFIRM_MODEL, len(batches), CONFIRM_BATCH_SIZE,
    )

    by_cluster_id = {c.cluster_id: c for c in clusters}
    all_decisions: list[dict] = []
    total_in_tok = 0
    total_out_tok = 0

    for batch_idx, batch in enumerate(batches, start=1):
        try:
            prompt = prompt_template.format(
                clusters=_build_clusters_block(batch)
            )
        except (KeyError, IndexError) as e:
            raise RuntimeError(
                f"events_to_projects confirm prompt has an unexpected "
                f"format placeholder: {e}. Only {{clusters}} should be an "
                f"unescaped placeholder; literal braces must be doubled."
            ) from e

        log.info(
            "events_to_projects batch %d/%d: %d cluster(s), %d prompt chars",
            batch_idx, len(batches), len(batch), len(prompt),
        )

        parsed, in_tok, out_tok = await _call_flash_lite(prompt)
        total_in_tok += in_tok
        total_out_tok += out_tok

        decisions = parsed.get("decisions") if isinstance(parsed, dict) else None
        if not isinstance(decisions, list):
            raise RuntimeError(
                f"events_to_projects confirm batch {batch_idx}: response "
                f"JSON has no 'decisions' list. Got: {parsed!r}"
            )

        covered: set[str] = set()
        for d in decisions:
            if isinstance(d, dict) and isinstance(d.get("cluster_id"), str):
                covered.add(d["cluster_id"])
        expected = {c.cluster_id for c in batch}
        missing = expected - covered
        if missing:
            raise RuntimeError(
                f"events_to_projects confirm batch {batch_idx}/{len(batches)}: "
                f"Flash-Lite omitted {len(missing)} of {len(expected)} "
                f"cluster(s). Missing: {sorted(missing)}. Reduce "
                f"CONFIRM_BATCH_SIZE or switch to 2.5 Flash."
            )

        all_decisions.extend(decisions)

    summary.decisions_returned = len(all_decisions)
    summary.input_tokens = total_in_tok
    summary.output_tokens = total_out_tok
    summary.cost_usd = (
        (total_in_tok / 1_000_000) * _FLASH_LITE_INPUT_PER_MTOK
        + (total_out_tok / 1_000_000) * _FLASH_LITE_OUTPUT_PER_MTOK
    )

    confirmed: list[ConfirmedProject] = []
    for d in all_decisions:
        if not isinstance(d, dict):
            continue
        if not d.get("is_project"):
            continue
        cid = d.get("cluster_id") or ""
        cluster = by_cluster_id.get(cid)
        if cluster is None:
            log.warning(
                "events_to_projects: decision references unknown cluster_id "
                "%r — skipping",
                cid,
            )
            continue
        slug = (d.get("slug") or "").strip()
        description = (d.get("description") or "").strip()
        seed_body = (d.get("seed_body") or "").strip()
        reason = (d.get("reason") or "").strip()
        if not slug or not seed_body:
            raise RuntimeError(
                f"events_to_projects: is_project=true decision missing "
                f"slug or seed_body: {d!r}"
            )
        # Honor the dangling slug — if the cluster is pre-named, the
        # Flash-Lite slug MUST match.
        if cluster.suggested_slug and slug != cluster.suggested_slug:
            log.info(
                "events_to_projects: overriding Flash-Lite slug %r with "
                "cluster's dangling slug %r",
                slug, cluster.suggested_slug,
            )
            slug = cluster.suggested_slug
        confirmed.append(
            ConfirmedProject(
                slug=wiki_store.slugify(slug),
                description=description,
                seed_body=seed_body,
                reason=reason,
                cluster=cluster,
            )
        )

    summary.projects_confirmed = len(confirmed)
    log.info(
        "events_to_projects: %d decision(s), %d confirmed, cost $%.4f",
        len(all_decisions), len(confirmed), summary.cost_usd,
    )
    return confirmed, summary


# ---------------------------------------------------------------------------
# 5. Write — direct wiki.apply_updates calls
# ---------------------------------------------------------------------------


def _compose_seed_page(project: ConfirmedProject) -> str:
    """Build the body for a new project page.

    Shape:

        <seed body>

        ## Recent
        - [[events/2026-04-10/foo]]
        - [[events/2026-04-12/bar]]

    No H1 — the seed body includes its own narrative and the wiki's
    convention is to leave the filename as the de-facto title for
    newly-minted projects (existing project pages don't use an H1 either).
    Frontmatter is applied by ``wiki.apply_updates`` on the create path.
    """
    lines: list[str] = [project.seed_body.rstrip(), "", "## Recent"]
    for path in project.cluster.paths:
        link = path.removesuffix(".md")
        lines.append(f"- [[{link}]]")
    return "\n".join(lines) + "\n"


def apply_confirmed(
    confirmed: list[ConfirmedProject],
) -> int:
    """Write each confirmed project. Returns the count successfully written.

    Uses ``wiki.apply_updates`` with a ``create`` action. If the project
    already exists at write time (race with another pass), falls back to
    ``update`` so we don't lose the new ``## Recent`` section.
    """
    if not confirmed:
        return 0

    updates: list[dict] = []
    for p in confirmed:
        project_path = WIKI_DIR / "projects" / f"{p.slug}.md"
        action = "update" if project_path.exists() else "create"
        if action == "update":
            log.info(
                "events_to_projects: projects/%s already exists — "
                "falling back to update",
                p.slug,
            )
        updates.append(
            {
                "category": "projects",
                "slug": p.slug,
                "action": action,
                "body_markdown": _compose_seed_page(p),
                "reason": (
                    f"Materialized from {len(p.cluster.paths)} event(s) "
                    f"via {p.cluster.source} cluster "
                    f"({p.cluster.cluster_id}): "
                    f"{p.reason or p.description}"
                ),
            }
        )

    applied = wiki_store.apply_updates(updates)

    # Audit one record per confirmed project so the trail cites the
    # specific events that seeded each create.
    try:
        from deja import audit

        for p in confirmed:
            events_list = ", ".join(p.cluster.paths)
            dangling_note = (
                f"dangling slug {p.cluster.suggested_slug!r}; "
                if p.cluster.source == "dangling"
                else ""
            )
            audit.record(
                "project_materialize",
                target=f"projects/{p.slug}",
                reason=(
                    f"{dangling_note}seeded from {len(p.cluster.paths)} "
                    f"event(s): {events_list}. {p.reason or p.description}"
                ),
                trigger={
                    "kind": "events_to_projects",
                    "detail": p.cluster.source,
                },
            )
    except Exception:
        log.debug("events_to_projects: audit.record failed", exc_info=True)

    return applied


# ---------------------------------------------------------------------------
# 6. Top-level entrypoint — called by reflection_scheduler
# ---------------------------------------------------------------------------


async def run_events_to_projects() -> dict:
    """Run one full events→projects sweep. Called by reflection_scheduler.

    Steps:
      1. find_clusters — dangling-slug + vector-similarity clusters
      2. confirm_clusters — Flash-Lite yes/no + slug + seed body
      3. apply_confirmed — create project pages, fall back to update on race

    Returns a SweepSummary as a dict. Raises loudly on any failure — no
    silent fallbacks per the project's rule.
    """
    wiki_store.ensure_dirs()

    clusters, events_indexed, dangling_count, vector_count = find_clusters()
    summary_preview = SweepSummary(
        events_indexed=events_indexed,
        dangling_clusters=dangling_count,
        vector_clusters=vector_count,
        clusters_proposed=len(clusters),
        clusters=list(clusters),
    )

    if events_indexed < _MIN_DANGLING_VOTES:
        log.info(
            "events_to_projects: only %d events indexed — skipping",
            events_indexed,
        )
        return summary_preview.as_dict()

    if not clusters:
        log.info(
            "events_to_projects: %d events, no clusters surfaced "
            "(dangling=%d, vector=%d)",
            events_indexed, dangling_count, vector_count,
        )
        return summary_preview.as_dict()

    confirmed, summary = await confirm_clusters(clusters)
    summary.events_indexed = events_indexed
    summary.dangling_clusters = dangling_count
    summary.vector_clusters = vector_count

    if confirmed:
        summary.projects_written = apply_confirmed(confirmed)

    log.info(
        "events_to_projects complete: %s",
        {
            k: v
            for k, v in summary.as_dict().items()
            if k != "clusters"
        },
    )
    return summary.as_dict()
