"""Prompt templates for deja LLM calls.

Prompts live in `~/Deja/prompts/<name>.md` and are edited
live in Obsidian. There is no fallback — if a prompt file is missing, this
raises FileNotFoundError loudly so the caller sees it immediately instead of
silently running on stale or absent instructions.
"""
from deja.config import WIKI_DIR
from pathlib import Path

WIKI_PROMPTS = WIKI_DIR / "prompts"


def load(name: str) -> str:
    """Return the contents of a prompt template. Raises if missing."""
    path = WIKI_PROMPTS / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(
            f"Prompt '{name}' not found at {path}. "
            f"Wiki prompts must live in {WIKI_PROMPTS}/"
        )
    return path.read_text()
