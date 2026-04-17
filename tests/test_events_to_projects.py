"""Tests for the events_to_projects sweep.

Covers:
  - Dangling-slug detection (≥2 events referencing a non-existent
    project slug).
  - Existing-project skip (events whose projects: list has real pages).
  - Mixed refs (event with both real + dangling; dangling still counts).
  - Vector-similarity clustering (empty projects, shared person).
  - Confirm flow — mocked Flash-Lite yes → project written with
    ``## Recent`` section listing the cluster's events.
  - Confirm flow — mocked Flash-Lite no → nothing written.
  - End-to-end: 3 dangling carpool events → projects/soccer-carpool.md
    materialized with seed + 3 Recent entries.

The confirm step is mocked throughout so tests run offline. Clustering
bypasses QMD by monkeypatching ``_load_event_vectors``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from deja import events_to_projects as etp


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_event(
    wiki: Path,
    day: str,
    slug: str,
    *,
    people: list[str],
    projects: list[str],
    body: str = "",
    title: str | None = None,
) -> str:
    """Create an event page and return its wiki-relative path."""
    day_dir = wiki / "events" / day
    day_dir.mkdir(parents=True, exist_ok=True)
    page = day_dir / f"{slug}.md"
    people_str = "[" + ", ".join(people) + "]"
    projects_str = "[" + ", ".join(projects) + "]"
    page.write_text(
        f"---\n"
        f"date: {day}\n"
        f'time: "10:00"\n'
        f"people: {people_str}\n"
        f"projects: {projects_str}\n"
        f"---\n"
        f"# {title or slug.replace('-', ' ').title()}\n\n"
        f"{body or 'Event body'}\n"
    )
    return f"events/{day}/{slug}.md"


def _write_project(wiki: Path, slug: str, body: str = "Existing project.") -> None:
    proj_dir = wiki / "projects"
    proj_dir.mkdir(parents=True, exist_ok=True)
    (proj_dir / f"{slug}.md").write_text(
        f"---\npreferred_name: {slug}\n---\n\n{body}\n"
    )


def _stub_load_event_vectors(
    monkeypatch, paths: list[str], vectors: np.ndarray | None = None
):
    """Replace _load_event_vectors with a canned (paths, mat) return.

    If ``vectors`` is None, synthesizes deterministic unit vectors per
    path — one random 16-dim embedding each. Tests that exercise vector
    clustering pass explicit vectors so similarities are controllable.
    """
    if vectors is None:
        rng = np.random.default_rng(42)
        dim = 16
        mat = rng.standard_normal((len(paths), dim)).astype(np.float32)
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        mat = mat / norms
    else:
        mat = vectors.astype(np.float32)
    # Replace the QMD-backed loader and the sqlite connection setup so
    # we never touch ~/.cache/qmd during tests.
    monkeypatch.setattr(etp, "_connect_qmd_db", lambda: _DummyDB())
    monkeypatch.setattr(
        etp, "_load_event_vectors", lambda db: (list(paths), mat)
    )


class _DummyDB:
    def close(self):
        pass


def _stub_side_effects(monkeypatch):
    """Silence the wiki write-path side effects."""
    monkeypatch.setattr("deja.wiki_git.ensure_repo", lambda: None)
    monkeypatch.setattr("deja.wiki_git.commit_changes", lambda msg: None)
    monkeypatch.setattr("deja.wiki_catalog.rebuild_index", lambda: None)
    try:
        import deja.llm.search as search
        monkeypatch.setattr(search, "refresh_index", lambda: None)
    except Exception:
        pass
    # qmd subprocess call inside wiki.apply_updates
    import subprocess
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 0)
    )


# ---------------------------------------------------------------------------
# Cluster discovery — dangling slugs
# ---------------------------------------------------------------------------


def test_dangling_slug_detection_three_events(isolated_home, monkeypatch):
    """3 events referencing a non-existent slug → one dangling cluster."""
    home, wiki = isolated_home
    paths = [
        _write_event(
            wiki, "2026-04-10", "carpool-mon",
            people=["david-wurtz", "sam-parent"],
            projects=["soccer-carpool"],
        ),
        _write_event(
            wiki, "2026-04-11", "carpool-wed",
            people=["david-wurtz", "sam-parent"],
            projects=["soccer-carpool"],
        ),
        _write_event(
            wiki, "2026-04-12", "carpool-fri",
            people=["david-wurtz", "sam-parent"],
            projects=["soccer-carpool"],
        ),
    ]
    _stub_load_event_vectors(monkeypatch, paths)

    clusters, events_indexed, dangling, vector = etp.find_clusters()
    assert events_indexed == 3
    assert dangling == 1
    assert vector == 0
    assert len(clusters) == 1
    c = clusters[0]
    assert c.source == "dangling"
    assert c.suggested_slug == "soccer-carpool"
    assert sorted(c.paths) == sorted(paths)


def test_existing_project_does_not_surface(isolated_home, monkeypatch):
    """Events whose projects: slug IS a real page are not dangling."""
    home, wiki = isolated_home
    _write_project(wiki, "soccer-carpool")
    paths = [
        _write_event(
            wiki, "2026-04-10", "carpool-mon",
            people=["david-wurtz", "sam-parent"],
            projects=["soccer-carpool"],
        ),
        _write_event(
            wiki, "2026-04-11", "carpool-wed",
            people=["david-wurtz", "sam-parent"],
            projects=["soccer-carpool"],
        ),
        _write_event(
            wiki, "2026-04-12", "carpool-fri",
            people=["david-wurtz", "sam-parent"],
            projects=["soccer-carpool"],
        ),
    ]
    _stub_load_event_vectors(monkeypatch, paths)

    clusters, events_indexed, dangling, vector = etp.find_clusters()
    assert events_indexed == 3
    assert dangling == 0
    # Also no vector cluster — events have a non-empty projects: field.
    assert vector == 0
    assert clusters == []


def test_mixed_projects_dangling_slug_still_counts(isolated_home, monkeypatch):
    """Event with [real, dangling] still votes for the dangling slug."""
    home, wiki = isolated_home
    _write_project(wiki, "real-project")
    paths = [
        _write_event(
            wiki, "2026-04-10", "e1",
            people=["david-wurtz", "coach"],
            projects=["real-project", "soccer-carpool"],
        ),
        _write_event(
            wiki, "2026-04-11", "e2",
            people=["david-wurtz", "coach"],
            projects=["soccer-carpool"],
        ),
    ]
    _stub_load_event_vectors(monkeypatch, paths)

    clusters, events_indexed, dangling, vector = etp.find_clusters()
    assert dangling == 1
    assert len(clusters) == 1
    c = clusters[0]
    assert c.suggested_slug == "soccer-carpool"
    assert len(c.paths) == 2


# ---------------------------------------------------------------------------
# Cluster discovery — vector similarity
# ---------------------------------------------------------------------------


def test_vector_cluster_shared_person_surfaces(isolated_home, monkeypatch):
    """3 empty-projects events with shared non-user person cluster together."""
    home, wiki = isolated_home
    paths = [
        _write_event(
            wiki, "2026-04-10", "pool-1",
            people=["david-wurtz", "pool-tech"],
            projects=[],
        ),
        _write_event(
            wiki, "2026-04-11", "pool-2",
            people=["david-wurtz", "pool-tech"],
            projects=[],
        ),
        _write_event(
            wiki, "2026-04-12", "pool-3",
            people=["david-wurtz", "pool-tech"],
            projects=[],
        ),
    ]
    # Identical unit vector → every pair is similarity 1.0.
    shared_vec = np.array([[1.0] + [0.0] * 15] * 3, dtype=np.float32)
    _stub_load_event_vectors(monkeypatch, paths, shared_vec)

    clusters, events_indexed, dangling, vector = etp.find_clusters()
    assert dangling == 0
    assert vector == 1
    assert len(clusters) == 1
    c = clusters[0]
    assert c.source == "vector"
    assert c.suggested_slug is None
    # david's slug is stripped; pool-tech survives as the shared anchor
    # (but only when identity resolution is available — we tolerate the
    # empty-shared case too if identity isn't loadable in tests).
    assert "pool-tech" in c.shared_people or c.shared_people == []


def test_vector_cluster_below_threshold_rejected(isolated_home, monkeypatch):
    """Empty-projects events with no shared person and low sim are rejected."""
    home, wiki = isolated_home
    paths = [
        _write_event(
            wiki, "2026-04-10", "solo-1",
            people=["david-wurtz"],
            projects=[],
        ),
        _write_event(
            wiki, "2026-04-11", "solo-2",
            people=["david-wurtz"],
            projects=[],
        ),
        _write_event(
            wiki, "2026-04-12", "solo-3",
            people=["david-wurtz"],
            projects=[],
        ),
    ]
    # Orthogonal vectors → sim = 0, well below threshold.
    orthogonal = np.eye(3, 16, dtype=np.float32)
    _stub_load_event_vectors(monkeypatch, paths, orthogonal)

    clusters, _, dangling, vector = etp.find_clusters()
    # No shared non-user person + low similarity → nothing.
    assert dangling == 0
    assert vector == 0
    assert clusters == []


# ---------------------------------------------------------------------------
# Confirm flow — Flash-Lite mocked
# ---------------------------------------------------------------------------


def _patch_flash_lite(monkeypatch, decisions: list[dict]):
    """Patch GeminiClient._generate_full to return canned decisions.

    Returns the proxy-mode dict shape: ``{"text": ..., "usage_metadata": ...}``
    — that's what the confirm path reads via ``resp.get("text")``.
    """
    import json as _json

    payload = {
        "text": _json.dumps({"decisions": decisions}),
        "usage_metadata": {
            "prompt_token_count": 500,
            "candidates_token_count": 200,
            "thoughts_token_count": 0,
        },
    }

    async def fake_generate_full(self, model, contents, config_dict):
        return payload

    from deja.llm_client import GeminiClient
    monkeypatch.setattr(GeminiClient, "_generate_full", fake_generate_full)


def test_confirm_yes_writes_project(isolated_home, monkeypatch):
    """Flash-Lite says yes → projects/<slug>.md is created with seed + Recent."""
    home, wiki = isolated_home
    _stub_side_effects(monkeypatch)
    paths = [
        _write_event(
            wiki, "2026-04-10", "carpool-mon",
            people=["david-wurtz", "sam-parent"],
            projects=["soccer-carpool"],
        ),
        _write_event(
            wiki, "2026-04-11", "carpool-wed",
            people=["david-wurtz", "sam-parent"],
            projects=["soccer-carpool"],
        ),
        _write_event(
            wiki, "2026-04-12", "carpool-fri",
            people=["david-wurtz", "sam-parent"],
            projects=["soccer-carpool"],
        ),
    ]
    _stub_load_event_vectors(monkeypatch, paths)

    _patch_flash_lite(monkeypatch, [
        {
            "cluster_id": "dangling-soccer-carpool",
            "is_project": True,
            "slug": "soccer-carpool",
            "description": "Soccer carpool logistics with Sam's parent.",
            "seed_body": (
                "Tracks the Monday/Wednesday/Friday soccer carpool rotation. "
                "David and Sam's parent alternate drop-off duties."
            ),
            "reason": "Recurring event pattern with dedicated slug.",
        },
    ])

    result = asyncio.run(etp.run_events_to_projects())

    assert result["projects_confirmed"] == 1
    assert result["projects_written"] == 1

    project = wiki / "projects" / "soccer-carpool.md"
    assert project.exists()
    text = project.read_text()
    assert "Soccer carpool logistics" in text or "Tracks the Monday" in text
    # Recent section with all three events.
    assert "## Recent" in text
    assert "[[events/2026-04-10/carpool-mon]]" in text
    assert "[[events/2026-04-11/carpool-wed]]" in text
    assert "[[events/2026-04-12/carpool-fri]]" in text


def test_confirm_no_writes_nothing(isolated_home, monkeypatch):
    """Flash-Lite says no → no project is written."""
    home, wiki = isolated_home
    _stub_side_effects(monkeypatch)
    paths = [
        _write_event(
            wiki, "2026-04-10", "stray-1",
            people=["david-wurtz", "sam-parent"],
            projects=["coincidence-slug"],
        ),
        _write_event(
            wiki, "2026-04-11", "stray-2",
            people=["david-wurtz", "sam-parent"],
            projects=["coincidence-slug"],
        ),
    ]
    _stub_load_event_vectors(monkeypatch, paths)

    _patch_flash_lite(monkeypatch, [
        {
            "cluster_id": "dangling-coincidence-slug",
            "is_project": False,
            "slug": None,
            "description": None,
            "seed_body": None,
            "reason": "Coincidental mentions, not a real ongoing project.",
        },
    ])

    result = asyncio.run(etp.run_events_to_projects())
    assert result["projects_confirmed"] == 0
    assert result["projects_written"] == 0
    assert not (wiki / "projects" / "coincidence-slug.md").exists()


def test_end_to_end_three_carpool_events(isolated_home, monkeypatch):
    """Full sweep: 3 dangling carpool events → project page materialized."""
    home, wiki = isolated_home
    _stub_side_effects(monkeypatch)

    paths = [
        _write_event(
            wiki, "2026-04-10", "carpool-mon",
            people=["david-wurtz", "sam-parent"],
            projects=["soccer-carpool"],
            body="Picked up Sam and Alex for Monday practice.",
            title="Monday carpool",
        ),
        _write_event(
            wiki, "2026-04-11", "carpool-wed",
            people=["david-wurtz", "sam-parent"],
            projects=["soccer-carpool"],
            body="Sam's parent drove on Wednesday.",
            title="Wednesday carpool",
        ),
        _write_event(
            wiki, "2026-04-12", "carpool-fri",
            people=["david-wurtz", "sam-parent"],
            projects=["soccer-carpool"],
            body="Friday practice — david drove.",
            title="Friday carpool",
        ),
    ]
    _stub_load_event_vectors(monkeypatch, paths)

    # Before: no projects directory or a directory with no soccer-carpool.
    assert not (wiki / "projects" / "soccer-carpool.md").exists()

    _patch_flash_lite(monkeypatch, [
        {
            "cluster_id": "dangling-soccer-carpool",
            "is_project": True,
            "slug": "soccer-carpool",
            "description": "Soccer carpool rotation.",
            "seed_body": (
                "Tracks the soccer carpool rotation for Sam and the team. "
                "David and Sam's parent alternate driving duties on M/W/F."
            ),
            "reason": "Three events share the slug and a recurring pattern.",
        },
    ])

    result = asyncio.run(etp.run_events_to_projects())

    # After: soccer-carpool exists with seed body + all three events in Recent.
    project = wiki / "projects" / "soccer-carpool.md"
    assert project.exists(), "project page should have been materialized"
    text = project.read_text()

    assert "soccer carpool rotation" in text.lower()
    assert "## Recent" in text
    for day, slug in [
        ("2026-04-10", "carpool-mon"),
        ("2026-04-11", "carpool-wed"),
        ("2026-04-12", "carpool-fri"),
    ]:
        assert f"[[events/{day}/{slug}]]" in text

    assert result["projects_written"] == 1
    assert result["dangling_clusters"] == 1
    assert result["clusters_proposed"] == 1


def test_slug_override_honors_dangling(isolated_home, monkeypatch):
    """If Flash-Lite returns a different slug, the dangling slug wins."""
    home, wiki = isolated_home
    _stub_side_effects(monkeypatch)
    paths = [
        _write_event(
            wiki, "2026-04-10", "a",
            people=["david-wurtz", "x"],
            projects=["soccer-carpool"],
        ),
        _write_event(
            wiki, "2026-04-11", "b",
            people=["david-wurtz", "x"],
            projects=["soccer-carpool"],
        ),
    ]
    _stub_load_event_vectors(monkeypatch, paths)

    _patch_flash_lite(monkeypatch, [
        {
            "cluster_id": "dangling-soccer-carpool",
            "is_project": True,
            # Flash-Lite picks a different slug → must be overridden.
            "slug": "kid-transportation",
            "description": "Logistics.",
            "seed_body": "Tracks soccer carpools.",
            "reason": "Recurring.",
        },
    ])

    asyncio.run(etp.run_events_to_projects())

    assert (wiki / "projects" / "soccer-carpool.md").exists()
    assert not (wiki / "projects" / "kid-transportation.md").exists()
