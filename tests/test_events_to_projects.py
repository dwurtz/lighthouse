"""Tests for the events_to_projects cluster-candidate generator.

Covers:
  - Dangling-slug detection (≥2 events referencing a non-existent
    project slug).
  - Existing-project skip (events whose projects: list has real pages).
  - Mixed refs (event with both real + dangling; dangling still counts).
  - Vector-similarity clustering (empty projects, shared person).
  - Low-similarity events without a shared person are rejected.

Clustering bypasses QMD by monkeypatching ``_load_event_vectors``.
The Flash-Lite confirm step and wiki write path are gone — cos now
owns those decisions and calls ``update_wiki`` directly.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

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
    monkeypatch.setattr(etp, "_connect_qmd_db", lambda: _DummyDB())
    monkeypatch.setattr(
        etp, "_load_event_vectors", lambda db: (list(paths), mat)
    )


class _DummyDB:
    def close(self):
        pass


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
    shared_vec = np.array([[1.0] + [0.0] * 15] * 3, dtype=np.float32)
    _stub_load_event_vectors(monkeypatch, paths, shared_vec)

    clusters, events_indexed, dangling, vector = etp.find_clusters()
    assert dangling == 0
    assert vector == 1
    assert len(clusters) == 1
    c = clusters[0]
    assert c.source == "vector"
    assert c.suggested_slug is None
    # david's slug is stripped when identity resolution is available;
    # pool-tech survives as the shared anchor (but we tolerate the
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
    orthogonal = np.eye(3, 16, dtype=np.float32)
    _stub_load_event_vectors(monkeypatch, paths, orthogonal)

    clusters, _, dangling, vector = etp.find_clusters()
    assert dangling == 0
    assert vector == 0
    assert clusters == []
