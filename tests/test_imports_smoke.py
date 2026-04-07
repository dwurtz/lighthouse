"""Smoke test: every top-level deja module imports without error.

Catches the class of bug where a rename or deleted helper leaves a
broken `from deja.foo import bar` somewhere — the monitor would
crash on boot. Running this before each restart beats finding out via
the notch icon going dark.
"""

from __future__ import annotations

import importlib
import pkgutil

import pytest


# Modules that pull in optional heavy deps or make network calls on import.
# Skip these from the smoke test — they're tested separately or gated by
# runtime availability.
SKIP = {
    "deja.observations.contacts",  # reads AddressBook SQLite on import
}


def _iter_modules():
    import deja
    for m in pkgutil.walk_packages(deja.__path__, prefix="deja."):
        if m.name in SKIP:
            continue
        yield m.name


@pytest.mark.parametrize("module_name", list(_iter_modules()))
def test_module_imports(module_name):
    importlib.import_module(module_name)


def test_monitor_main_entrypoint_imports():
    # The `python -m deja monitor` path
    import deja.__main__  # noqa: F401


def test_monitor_loop_class_instantiable():
    from deja.agent.loop import AgentLoop
    # We can't actually construct it without a GeminiClient (needs API key),
    # but we can verify the class is importable and has expected attrs
    assert hasattr(AgentLoop, "run")
    assert hasattr(AgentLoop, "stop")
    assert hasattr(AgentLoop, "_analysis_cycle")
