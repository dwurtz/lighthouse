"""Prompt templates for deja LLM calls.

Prompts are bundled inside the package at ``default_assets/prompts/``.
This is the single source of truth — Sparkle app updates deliver new
prompt versions automatically, and there's no drift between "what the
developer committed" and "what the user's install is running."

``load(name)`` reads the bundled version. No wiki copy, no override
mechanism, no ``~/Deja/prompts/`` directory needed.
"""

import importlib.resources as pkg_resources


def load(name: str) -> str:
    """Return the contents of a bundled prompt template.

    Raises ``FileNotFoundError`` if the prompt doesn't exist in the
    package — that's a packaging bug, not a user-facing problem.
    """
    src = pkg_resources.files("deja") / "default_assets" / "prompts" / f"{name}.md"
    if not src.is_file():
        raise FileNotFoundError(
            f"Prompt '{name}' not found in bundled default_assets. "
            f"This is a packaging bug — the prompt should exist at "
            f"default_assets/prompts/{name}.md inside the deja package."
        )
    return src.read_text()
