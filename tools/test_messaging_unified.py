#!/usr/bin/env python3
"""End-to-end smoke test for the per-turn messaging unification.

Feeds a mock iMessage buffer containing a 3-person group chat (David,
Laura, Dominique — 5 turns) through collector → tiering → format and
asserts per-turn attribution survives the pipeline. Exists so a human
can eyeball the rendered output and confirm the fix in one go.

Run:
    python tools/test_messaging_unified.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

# Point DEJA_HOME / DEJA_WIKI at a tmp dir BEFORE importing deja modules
# so the isolated paths stick through all downstream imports.
_tmp = tempfile.mkdtemp(prefix="deja-smoke-")
_home = Path(_tmp) / "deja_home"
_wiki = Path(_tmp) / "wiki"
_home.mkdir()
_wiki.mkdir()
os.environ["DEJA_HOME"] = str(_home)
os.environ["DEJA_WIKI"] = str(_wiki)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from deja.observations import imessage  # noqa: E402
from deja.observations.types import Observation  # noqa: E402
from deja.signals import format as fmt, tiering  # noqa: E402


def _mk_buffer(path: Path) -> None:
    """Write a fabricated 3-person group-chat buffer with 5 per-turn rows."""
    now = datetime(2026, 4, 16, 10, 0, 0)

    def dt(offset_sec: int) -> tuple[str, float]:
        t = now + timedelta(seconds=offset_sec)
        return t.strftime("%Y-%m-%d %H:%M:%S"), t.timestamp()

    rows = []
    for offset, speaker, text in [
        (0,  "+15553334444", "who has Sansita's address?"),             # Laura
        (30, "me",           "checking"),                                # David
        (60, "+15553334444", "here it is: 123 Main St, Brooklyn"),       # Laura
        (90, "+15551112222", "perfect, thanks"),                         # Dominique
        (120, "me",          "great — on my way"),                       # David
    ]:
        d, ts = dt(offset)
        rows.append({
            "text": text,
            "timestamp": ts,
            "dt": d,
            "is_from_me": speaker == "me",
            "sender": "Nie",                 # chat label (back-compat mirror)
            "chat_id": "imsg-chat-42",
            "chat_label": "Nie",
            "speaker": speaker,
        })
    path.write_text(json.dumps(rows))


def _setup_wiki(wiki: Path) -> None:
    """Drop an inner-circle entry for Dominique so the tiering call has
    something to match against."""
    people = wiki / "people"
    people.mkdir(exist_ok=True)
    (people / "dominique.md").write_text(
        "---\n"
        "name: Dominique\n"
        "inner_circle: true\n"
        "phones: ['+15551112222']\n"
        "---\n"
        "# Dominique\n"
    )
    tiering.reset_caches()


def main() -> int:
    print(f"[smoke] tmp home: {_home}")
    print(f"[smoke] tmp wiki: {_wiki}")

    _setup_wiki(_wiki)

    buffer_path = _home / "imessage_buffer.json"
    _mk_buffer(buffer_path)

    # Patch the buffer path the collector reads. The collector captured
    # _IMESSAGE_BUFFER at import time from the pre-env-set DEJA_HOME.
    imessage._IMESSAGE_BUFFER = buffer_path

    obs = imessage._collect_imessages(limit=100)
    print(f"\n[smoke] collected {len(obs)} observations from buffer")

    # Check: 5 observations, all same chat_id, correct per-speaker attribution.
    assert len(obs) == 5, f"expected 5 per-turn observations, got {len(obs)}"
    chat_ids = {o.chat_id for o in obs}
    assert chat_ids == {"imsg-chat-42"}, f"chat_ids drifted: {chat_ids}"

    speakers = [o.speaker for o in obs]
    print(f"[smoke] speakers: {speakers}")
    assert "You" in speakers, "outbound was not rewritten to 'You'"
    assert any("+15551112222" in (s or "") for s in speakers), \
        "Dominique's raw handle not preserved in speaker"
    assert any("+15553334444" in (s or "") for s in speakers), \
        "Laura's raw handle not preserved in speaker"

    # Tier each observation and print.
    print("\n[smoke] tier breakdown:")
    tier_counts = {1: 0, 2: 0, 3: 0}
    for o in obs:
        d = {
            "source": o.source,
            "sender": o.sender,
            "chat_id": o.chat_id,
            "chat_label": o.chat_label,
            "speaker": o.speaker,
            "text": o.text,
        }
        tier = tiering.classify_tier(d)
        tier_counts[tier] += 1
        print(f"  T{tier}  speaker={o.speaker!r:<30}  text={o.text!r}")

    # 2 outbound (David) + 1 inner-circle (Dominique) = 3 Tier-1 turns.
    # 2 non-inner-circle inbound (Laura) = 2 Tier-3 turns.
    assert tier_counts[1] == 3, f"expected 3 Tier-1 turns, got {tier_counts[1]}"
    assert tier_counts[3] == 2, f"expected 2 Tier-3 turns, got {tier_counts[3]}"

    # Render through format_signals — fresh (empty) observations.jsonl so
    # no prior context is prepended; we just want to see the per-turn
    # lines render with per-speaker attribution.
    jsonl = _home / "observations.jsonl"
    jsonl.write_text("")
    fmt.OBSERVATIONS_LOG = jsonl

    dicts = [
        {
            "source": o.source,
            "sender": o.sender,
            "chat_id": o.chat_id,
            "chat_label": o.chat_label,
            "speaker": o.speaker,
            "text": o.text,
            "timestamp": o.timestamp.isoformat(),
            "id_key": o.id_key,
        }
        for o in obs
    ]
    rendered = fmt.format_signals(dicts)
    print("\n[smoke] rendered timeline:")
    print(rendered)

    # Every line must carry the chat label AND the speaker — this is the
    # disambiguation that the fabrication bug was missing.
    for line in rendered.split("\n"):
        assert "Nie /" in line, f"rendered line missing chat_label/speaker split: {line!r}"

    print("\n[smoke] OK — all assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
