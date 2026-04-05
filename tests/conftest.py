"""Shared test fixtures.

Every test runs against throwaway LIGHTHOUSE_HOME and LIGHTHOUSE_WIKI dirs so
nothing in ~/.lighthouse or ~/Lighthouse Wiki is touched.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_home(request, monkeypatch, tmp_path):
    """Point LIGHTHOUSE_HOME and LIGHTHOUSE_WIKI at tmp dirs for every test.

    Config reads these at import time, so we also patch the already-imported
    module attributes for any modules that cached them.

    Tests marked ``@pytest.mark.real_wiki`` (or with the ``vision`` marker)
    opt out — they need the real wiki prompts and index.md to exercise the
    live vision pipeline.
    """
    if request.node.get_closest_marker("real_wiki") or request.node.get_closest_marker("vision"):
        yield None
        return

    home = tmp_path / "workagent_home"
    wiki = tmp_path / "wiki"
    home.mkdir()
    wiki.mkdir()

    monkeypatch.setenv("LIGHTHOUSE_HOME", str(home))
    monkeypatch.setenv("LIGHTHOUSE_WIKI", str(wiki))

    # Patch modules that cached the paths at import time.
    import lighthouse.config as config
    monkeypatch.setattr(config, "LIGHTHOUSE_HOME", home)
    monkeypatch.setattr(config, "WIKI_DIR", wiki)

    import lighthouse.wiki as wiki_mod
    monkeypatch.setattr(wiki_mod, "WIKI_DIR", wiki)

    import lighthouse.activity_log as wiki_log
    monkeypatch.setattr(wiki_log, "LOG_PATH", wiki / "log.md")

    # Modules that did `from lighthouse.config import LIGHTHOUSE_HOME` captured
    # the original path at import time — patch their local bindings too.
    import lighthouse.observations.collector as collector_mod
    monkeypatch.setattr(collector_mod, "LIGHTHOUSE_HOME", home)

    import lighthouse.health_check as sc_mod
    monkeypatch.setattr(sc_mod, "WIKI_DIR", wiki)

    import lighthouse.identity as user_mod
    monkeypatch.setattr(user_mod, "WIKI_DIR", wiki)

    yield home, wiki
