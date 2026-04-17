"""Diff Claude-local shadow vs Gemini production output, per cycle.

Walks ``~/.deja/integrate_shadow/<ts>.json`` records (written when
``INTEGRATE_CLAUDE_SHADOW=true`` is set in config), extracts the
production Gemini record and the ``claude-local`` shadow record,
and prints a side-by-side summary plus aggregate stats.

Usage:
    uv run python tools/integrate_shadow_diff.py            # all cycles
    uv run python tools/integrate_shadow_diff.py --since 6  # last 6 hours
    uv run python tools/integrate_shadow_diff.py --latest 5 # last 5 cycles
    uv run python tools/integrate_shadow_diff.py --aggregate-only
    uv run python tools/integrate_shadow_diff.py path/to/single.json  # one file

Look for:
  * Cycles where Claude wrote FEWER wiki updates — may indicate Claude
    filtered noise Gemini wrote (desirable) OR Claude missed real
    signals (undesirable).
  * Cycles where Claude wrote MORE — possible noise, or Claude noticing
    something Gemini didn't.
  * Difference in observation_narratives — wording quality + accuracy.
  * Slugs unique to one side — each one is a judgment call we need to
    audit manually.
  * Latency — Claude is expected to be 5-10× slower. Sanity-check it's
    under ~45s typically; over that and we'd burn the 5-min cycle.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import Counter

SHADOW_DIR = Path.home() / ".deja/integrate_shadow"


def _load(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[skip] {path.name}: parse error ({e})", file=sys.stderr)
        return None


def _iter_records(since_hours: int | None = None, latest: int | None = None):
    """Yield (path, record) pairs newest-first, filtered by --since / --latest."""
    if not SHADOW_DIR.exists():
        return
    files = sorted(SHADOW_DIR.glob("*.json"), reverse=True)
    if since_hours is not None:
        cutoff = datetime.now() - timedelta(hours=since_hours)
        files = [f for f in files if datetime.fromtimestamp(f.stat().st_mtime) >= cutoff]
    if latest is not None:
        files = files[:latest]
    for f in files:
        rec = _load(f)
        if rec is not None:
            yield f, rec


def _claude_shadow(record: dict) -> dict | None:
    for s in record.get("shadows") or []:
        if s.get("model") == "claude-local":
            return s
    return None


def _slugs(updates: list) -> list[str]:
    out = []
    for u in updates or []:
        cat = u.get("category", "")
        slug = u.get("slug", "")
        if cat and slug:
            out.append(f"{cat}/{slug}")
    return out


def _fmt_updates(updates: list, indent: str = "    ") -> str:
    if not updates:
        return f"{indent}(none)"
    lines = []
    for u in updates:
        action = u.get("action", "?")
        cat = u.get("category", "?")
        slug = u.get("slug", "?")
        reason = (u.get("reason") or "")[:120]
        lines.append(f"{indent}[{action}] {cat}/{slug} — {reason}")
    return "\n".join(lines)


def print_side_by_side(path: Path, record: dict) -> None:
    prod = record.get("production") or {}
    shadow = _claude_shadow(record)

    ts = record.get("timestamp", "")[:19].replace("T", " ")
    print(f"\n═══ {path.name}   {ts} ═══")

    prod_slugs = set(_slugs(prod.get("wiki_updates", [])))
    if shadow is None or "error" in shadow:
        err = (shadow or {}).get("error", "no claude-local shadow in this record")
        print(f"  claude-local: {err}")
        return

    claude_slugs = set(_slugs(shadow.get("wiki_updates", [])))
    only_prod = prod_slugs - claude_slugs
    only_claude = claude_slugs - prod_slugs
    both = prod_slugs & claude_slugs

    print(f"  Gemini {prod.get('model','?')}  ({prod.get('latency_ms','?')}ms, "
          f"{len(prod.get('wiki_updates',[]))} updates)")
    print(_fmt_updates(prod.get("wiki_updates", []), indent="    "))
    print(f"  Claude (local)  ({shadow.get('latency_ms','?')}ms, "
          f"{len(shadow.get('wiki_updates',[]))} updates)")
    print(_fmt_updates(shadow.get("wiki_updates", []), indent="    "))

    if only_prod:
        print(f"\n  ⚠ only Gemini: {sorted(only_prod)}")
    if only_claude:
        print(f"  ⚠ only Claude: {sorted(only_claude)}")
    if both and not (only_prod or only_claude):
        print(f"  ✓ agreement ({len(both)} slug(s))")

    if prod.get("observation_narrative") or shadow.get("observation_narrative"):
        print("\n  Narratives:")
        print(f"    Gemini: {(prod.get('observation_narrative') or '')[:400]}")
        print(f"    Claude: {(shadow.get('observation_narrative') or '')[:400]}")


def aggregate(records: list[tuple[Path, dict]]) -> None:
    if not records:
        print("(no shadow records found)")
        return

    total = len(records)
    claude_present = 0
    claude_errors = 0
    agree = 0
    claude_only = 0
    gemini_only = 0
    narrative_divergence = 0
    prod_updates_sum = 0
    claude_updates_sum = 0
    prod_latency_sum = 0
    claude_latency_sum = 0

    for _path, rec in records:
        prod = rec.get("production") or {}
        shadow = _claude_shadow(rec)
        if shadow is None:
            continue
        claude_present += 1
        if "error" in shadow:
            claude_errors += 1
            continue

        prod_slugs = set(_slugs(prod.get("wiki_updates", [])))
        claude_slugs = set(_slugs(shadow.get("wiki_updates", [])))
        if prod_slugs == claude_slugs:
            agree += 1
        else:
            if prod_slugs - claude_slugs:
                gemini_only += 1
            if claude_slugs - prod_slugs:
                claude_only += 1

        if (prod.get("observation_narrative") or "") != (shadow.get("observation_narrative") or ""):
            narrative_divergence += 1

        prod_updates_sum += len(prod.get("wiki_updates", []))
        claude_updates_sum += len(shadow.get("wiki_updates", []))
        prod_latency_sum += int(prod.get("latency_ms") or 0)
        claude_latency_sum += int(shadow.get("latency_ms") or 0)

    print("\n════════════ aggregate ════════════")
    print(f"  cycles scanned:           {total}")
    print(f"  cycles with claude shadow: {claude_present}")
    print(f"  claude shadow errors:      {claude_errors}")
    healthy = claude_present - claude_errors
    if healthy:
        print(f"  full-agreement cycles:     {agree}/{healthy}  ({100*agree//healthy}%)")
        print(f"  cycles Gemini wrote slugs Claude didn't:  {gemini_only}")
        print(f"  cycles Claude wrote slugs Gemini didn't:  {claude_only}")
        print(f"  narratives differ:         {narrative_divergence}/{healthy}")
        print(f"  avg wiki_updates/cycle:    Gemini {prod_updates_sum/healthy:.1f}  "
              f"Claude {claude_updates_sum/healthy:.1f}")
        print(f"  avg latency (ms):          Gemini {prod_latency_sum/healthy:.0f}  "
              f"Claude {claude_latency_sum/healthy:.0f}")
    print()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("path", nargs="?", help="Specific shadow JSON file (default: scan all)")
    ap.add_argument("--since", type=int, help="Only cycles from the last N hours")
    ap.add_argument("--latest", type=int, help="Only the latest N cycles")
    ap.add_argument("--aggregate-only", action="store_true", help="Skip per-cycle output")
    args = ap.parse_args()

    if args.path:
        path = Path(args.path)
        if not path.exists():
            print(f"not found: {path}", file=sys.stderr)
            sys.exit(1)
        rec = _load(path)
        if rec:
            print_side_by_side(path, rec)
            aggregate([(path, rec)])
        return

    records = list(_iter_records(since_hours=args.since, latest=args.latest))
    if not args.aggregate_only:
        # Iterate oldest-first so console read-top-to-bottom matches time.
        for path, rec in reversed(records):
            print_side_by_side(path, rec)
    aggregate(records)


if __name__ == "__main__":
    main()
