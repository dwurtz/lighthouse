"""Reconstruct a timeline for a given request id.

Given a ``req_xxx`` identifier from a user's error toast, pull every
matching row from the local Deja trace files and print them in
chronological order so support can see exactly what happened.

Sources:

  * ``~/.deja/deja.log``           — log lines prefixed with ``[req_xxx]``
  * ``~/.deja/audit.jsonl``        — audit rows with ``request_id`` field
  * ``~/.deja/errors.jsonl``       — error rows with ``request_id`` field
  * ``~/.deja/integrate_shadow/``  — per-cycle shadow eval records (best-
                                     effort match: explicit ``request_id``
                                     field, otherwise skipped)

Usage:

    ./venv/bin/python tools/deja_support_lookup.py req_abc123def456
    ./venv/bin/python tools/deja_support_lookup.py req_xxx --json
    ./venv/bin/python tools/deja_support_lookup.py req_xxx --since 2026-04-01
    ./venv/bin/python tools/deja_support_lookup.py req_xxx --path /tmp/uploaded

Exits 0 if the request id left a trace anywhere, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


# ---------------------------------------------------------------------------
# Log line parsing
# ---------------------------------------------------------------------------

# Standard Python logging default format: "YYYY-MM-DD HH:MM:SS,ms name LEVEL msg"
_LOG_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)")


def _extract_log_timestamp(line: str) -> str:
    m = _LOG_TS_RE.match(line)
    return m.group(1) if m else ""


def _normalize_ts(ts: str) -> str:
    """Return a lexically-sortable timestamp string (best effort).

    We keep original formatting in output but use this key for ordering.
    """
    if not ts:
        return ""
    # Python logging uses a comma before milliseconds; normalize to '.'.
    return ts.replace(",", ".").replace(" ", "T")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Event:
    ts: str                     # display timestamp
    sort_key: str               # normalized for ordering
    source: str                 # LOG / AUDIT / ERROR / SHADOW
    summary: str                # one-line rendering
    raw: Any = field(default=None)


# ---------------------------------------------------------------------------
# Collectors
# ---------------------------------------------------------------------------

def _collect_log(path: Path, rid: str) -> list[Event]:
    if not path.exists():
        return []
    out: list[Event] = []
    needle = f"[{rid}]"
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if needle not in line:
                    continue
                line = line.rstrip("\n")
                ts = _extract_log_timestamp(line)
                out.append(Event(
                    ts=ts,
                    sort_key=_normalize_ts(ts),
                    source="LOG",
                    summary=line,
                    raw=line,
                ))
    except OSError:
        return []
    return out


def _collect_jsonl(path: Path, rid: str, source: str) -> list[Event]:
    if not path.exists():
        return []
    out: list[Event] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if row.get("request_id") != rid:
                    continue
                ts = row.get("ts") or row.get("timestamp") or ""
                out.append(Event(
                    ts=ts,
                    sort_key=_normalize_ts(ts),
                    source=source,
                    summary=_summarize_row(row, source),
                    raw=row,
                ))
    except OSError:
        return []
    return out


def _collect_shadow(dirpath: Path, rid: str) -> list[Event]:
    if not dirpath.exists() or not dirpath.is_dir():
        return []
    out: list[Event] = []
    for p in sorted(dirpath.glob("*.json")):
        try:
            row = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        # Only match on an explicit request_id field — we don't guess.
        if row.get("request_id") != rid:
            continue
        ts = row.get("timestamp") or ""
        out.append(Event(
            ts=ts,
            sort_key=_normalize_ts(ts),
            source="SHADOW",
            summary=f"shadow cycle file={p.name}",
            raw=row,
        ))
    return out


def _summarize_row(row: dict, source: str) -> str:
    if source == "AUDIT":
        action = row.get("action", "?")
        target = row.get("target", "?")
        reason = row.get("reason", "")
        trigger = (row.get("trigger") or {}).get("kind", "")
        cycle = row.get("cycle", "")
        extra = []
        if trigger:
            extra.append(f"trigger={trigger}")
        if cycle:
            extra.append(f"cycle={cycle}")
        meta = " ".join(extra)
        return f"{action} {target}" + (f"  ({meta})" if meta else "") + \
               (f"  -- {reason}" if reason else "")
    if source == "ERROR":
        code = row.get("code", "?")
        msg = row.get("message", "")
        details = row.get("details") or {}
        tail = f" details={json.dumps(details, default=str)}" if details else ""
        return f"[{code}] {msg}{tail}"
    return json.dumps(row, default=str)


# ---------------------------------------------------------------------------
# Filtering / ordering
# ---------------------------------------------------------------------------

def _filter_since(events: list[Event], since: str) -> list[Event]:
    if not since:
        return events
    key = _normalize_ts(since)
    return [e for e in events if e.sort_key >= key]


def _sort(events: list[Event]) -> list[Event]:
    # Stable sort so files with equal timestamps keep a sensible order:
    # LOG < AUDIT < ERROR < SHADOW.
    rank = {"LOG": 0, "AUDIT": 1, "ERROR": 2, "SHADOW": 3}
    return sorted(events, key=lambda e: (e.sort_key, rank.get(e.source, 9)))


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _render_prose(rid: str, events: Iterable[Event]) -> str:
    lines = [f"timeline for {rid}", "=" * (13 + len(rid))]
    for e in events:
        ts = e.ts or "-"
        lines.append(f"{ts:<26} {e.source:<7} {e.summary}")
    return "\n".join(lines) + "\n"


def _render_json(rid: str, events: list[Event]) -> str:
    payload = {
        "request_id": rid,
        "event_count": len(events),
        "events": [
            {
                "ts": e.ts,
                "source": e.source,
                "summary": e.summary,
                "raw": e.raw,
            }
            for e in events
        ],
    }
    return json.dumps(payload, indent=2, default=str) + "\n"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def lookup(
    request_id: str,
    *,
    base: Path,
    since: str = "",
    limit: int = 0,
) -> list[Event]:
    """Collect all events for ``request_id`` under ``base``."""
    events: list[Event] = []
    events.extend(_collect_log(base / "deja.log", request_id))
    events.extend(_collect_jsonl(base / "audit.jsonl", request_id, "AUDIT"))
    events.extend(_collect_jsonl(base / "errors.jsonl", request_id, "ERROR"))
    events.extend(_collect_shadow(base / "integrate_shadow", request_id))
    events = _filter_since(events, since)
    events = _sort(events)
    if limit and limit > 0:
        events = events[:limit]
    return events


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Reconstruct a Deja request-id timeline for support."
    )
    ap.add_argument("request_id", help="The req_xxx identifier to trace.")
    ap.add_argument("--json", action="store_true",
                    help="Emit one JSON object instead of prose.")
    ap.add_argument("--since", default="",
                    help="Only include events at/after this timestamp "
                         "(ISO-ish; lexical compare).")
    ap.add_argument("--limit", type=int, default=0,
                    help="Cap the number of events returned.")
    ap.add_argument("--path", default=str(Path.home() / ".deja"),
                    help="Base directory (default: ~/.deja). Point at an "
                         "unpacked support zip for offline triage.")
    args = ap.parse_args(argv)

    rid = args.request_id.strip()
    if not rid:
        print("error: empty request id", file=sys.stderr)
        return 2

    base = Path(args.path).expanduser()
    events = lookup(rid, base=base, since=args.since, limit=args.limit)

    if args.json:
        sys.stdout.write(_render_json(rid, events))
    else:
        sys.stdout.write(_render_prose(rid, events))

    return 0 if events else 1


if __name__ == "__main__":
    raise SystemExit(main())
