"""Live-loaded wiki schema document.

The schema (``CLAUDE.md`` at the root of the Déjà Wiki) encodes David's
conventions for how the wiki should be written and maintained. It lives inside
the Obsidian vault so David can edit it directly, and we read it on every call
so his edits take effect immediately — no caching, no reload logic, no
fallback. If ``CLAUDE.md`` is missing, this raises loudly so the caller sees
the misconfiguration instead of silently running on no schema.
"""

from deja.config import WIKI_DIR
from pathlib import Path

SCHEMA_PATH = WIKI_DIR / "CLAUDE.md"


def load_schema() -> str:
    """Return the contents of ``CLAUDE.md``. Raises if missing."""
    if not SCHEMA_PATH.exists():
        raise FileNotFoundError(
            f"Wiki schema not found at {SCHEMA_PATH}. "
            f"CLAUDE.md must exist in the wiki root."
        )
    return SCHEMA_PATH.read_text(encoding="utf-8").strip()
