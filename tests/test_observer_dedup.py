"""Observer: dedup + analysis offset marker.

Instantiates the collector without running any real collectors (they all
raise when deps are mocked out) — we just exercise the dedup set and the
byte-offset marker, which are the two places a regression would silently
drop real signals or re-feed old ones.
"""

from __future__ import annotations

from datetime import datetime

from deja.observations.collector import Observer
from deja.observations.types import Observation


def _mk_signal(id_key: str, source: str = "imessage", text: str = "hi") -> Observation:
    return Observation(
        source=source,
        sender="Alice",
        text=text,
        timestamp=datetime.now(),
        id_key=id_key,
    )


def test_persist_and_load_history(isolated_home):
    home, _ = isolated_home
    c = Observer()
    s1 = _mk_signal("a")
    s2 = _mk_signal("b")
    c._persist_signal(s1)
    c._persist_signal(s2)

    # New collector should hydrate from the jsonl on disk
    c2 = Observer()
    assert "a" in c2._seen_ids
    assert "b" in c2._seen_ids
    assert len(c2.recent_history) == 2


def test_analysis_offset_marker_round_trip(isolated_home):
    c = Observer()
    c._persist_signal(_mk_signal("a"))
    c._persist_signal(_mk_signal("b"))

    items, new_offset = c.get_unanalyzed_signals_structured()
    assert len(items) == 2
    assert new_offset > 0

    c.save_analysis_marker(new_offset)

    # Nothing new → empty batch
    items2, offset2 = c.get_unanalyzed_signals_structured()
    assert items2 == []
    assert offset2 == new_offset

    # Persist another signal → only that one comes back
    c._persist_signal(_mk_signal("c"))
    items3, offset3 = c.get_unanalyzed_signals_structured()
    assert len(items3) == 1
    assert items3[0]["id_key"] == "c"
    assert offset3 > new_offset


def test_get_recent_signals_from_log_filters_by_time(isolated_home):
    from datetime import timedelta
    c = Observer()
    # Manually craft an old signal by writing to the log directly
    import json
    old_ts = (datetime.now() - timedelta(hours=2)).isoformat()
    new_ts = datetime.now().isoformat()
    with open(c._signal_log_path, "a") as f:
        f.write(json.dumps({
            "source": "imessage", "sender": "old", "text": "old msg",
            "timestamp": old_ts, "id_key": "old1",
        }) + "\n")
        f.write(json.dumps({
            "source": "imessage", "sender": "new", "text": "new msg",
            "timestamp": new_ts, "id_key": "new1",
        }) + "\n")

    text = c.get_recent_signals_from_log(minutes=5)
    assert "new msg" in text
    assert "old msg" not in text


def test_should_screenshot_detects_changes(isolated_home):
    c = Observer()
    assert c.should_screenshot("Chrome", "Google") is True
    # Same app+title → no change
    assert c.should_screenshot("Chrome", "Google") is False
    # Title change → screenshot
    assert c.should_screenshot("Chrome", "Gmail") is True
    # App change → screenshot
    assert c.should_screenshot("Slack", "Gmail") is True
