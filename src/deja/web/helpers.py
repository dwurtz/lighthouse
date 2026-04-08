"""Shared helpers used by multiple route modules."""

from __future__ import annotations

import json
from pathlib import Path

from deja.config import DEJA_HOME

OBSERVATIONS_LOG = DEJA_HOME / "observations.jsonl"
INTEGRATIONS_LOG = DEJA_HOME / "integrations.jsonl"
CONVERSATION_PATH = DEJA_HOME / "conversation.json"


def read_jsonl(path: Path, limit: int | None = None) -> list[dict]:
    if not path.exists():
        return []
    entries: list[dict] = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    if limit is not None:
        entries = entries[-limit:]
    return entries


def load_conversation() -> list[dict]:
    if not CONVERSATION_PATH.exists():
        return []
    try:
        return json.loads(CONVERSATION_PATH.read_text())
    except (json.JSONDecodeError, ValueError):
        return []


def save_conversation(messages: list[dict]) -> None:
    CONVERSATION_PATH.write_text(json.dumps(messages, indent=2))
