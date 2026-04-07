"""Shared test fixtures.

Every test runs against throwaway DEJA_HOME and DEJA_WIKI dirs so
nothing in ~/.deja or ~/Deja is touched.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_home(request, monkeypatch, tmp_path):
    """Point DEJA_HOME and DEJA_WIKI at tmp dirs for every test.

    Config reads these at import time, so we also patch the already-imported
    module attributes for any modules that cached them.

    Tests marked ``@pytest.mark.real_wiki`` (or with the ``vision`` marker)
    opt out — they need the real wiki prompts and index.md to exercise the
    live vision pipeline.
    """
    if request.node.get_closest_marker("real_wiki") or request.node.get_closest_marker("vision"):
        yield None
        return

    home = tmp_path / "deja_home"
    wiki = tmp_path / "wiki"
    home.mkdir()
    wiki.mkdir()

    monkeypatch.setenv("DEJA_HOME", str(home))
    monkeypatch.setenv("DEJA_WIKI", str(wiki))

    # Patch modules that cached the paths at import time.
    import deja.config as config
    monkeypatch.setattr(config, "DEJA_HOME", home)
    monkeypatch.setattr(config, "LIGHTHOUSE_HOME", home)  # back-compat alias
    monkeypatch.setattr(config, "WIKI_DIR", wiki)

    import deja.wiki as wiki_mod
    monkeypatch.setattr(wiki_mod, "WIKI_DIR", wiki)

    import deja.activity_log as wiki_log
    monkeypatch.setattr(wiki_log, "LOG_PATH", wiki / "log.md")

    # Modules that did `from deja.config import DEJA_HOME` captured
    # the original path at import time — patch their local bindings too.
    import deja.observations.collector as collector_mod
    monkeypatch.setattr(collector_mod, "DEJA_HOME", home)

    import deja.health_check as sc_mod
    monkeypatch.setattr(sc_mod, "WIKI_DIR", wiki)

    import deja.identity as user_mod
    monkeypatch.setattr(user_mod, "WIKI_DIR", wiki)

    yield home, wiki
