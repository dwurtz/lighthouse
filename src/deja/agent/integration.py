"""Analysis audit log.

Appends one JSON entry per analysis cycle to ~/.deja/integrations.jsonl.
This is the machine-readable record the Swift notch Activity tab reads to
render insights. The human-readable equivalent is log.md inside the wiki.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from deja.config import DEJA_HOME

log = logging.getLogger(__name__)


def log_analysis(
    matches,
    skips,
    new_facts,
    commitments,
    events,
    proposed_goals,
    conversations,
    questions,
) -> None:
    """Write one analysis cycle result to integrations.jsonl.

    The fields are kept for Swift UI compatibility — most will be empty
    lists in the current architecture. The legacy shape is what the notch
    Activity tab knows how to render.
    """
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "matches": matches,
        "skips": skips[:10],
        "new_facts": new_facts,
        "commitments": commitments,
        "events": events,
        "proposed_goals": proposed_goals,
        "conversations": conversations,
        "questions": questions,
    }
    try:
        DEJA_HOME.mkdir(parents=True, exist_ok=True)
        with open(DEJA_HOME / "integrations.jsonl", "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        log.exception("Failed to write integrations.jsonl")
