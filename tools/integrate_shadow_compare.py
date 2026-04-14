"""Compare integrate cycles between production and one or more shadow models.

Reads every file in ``~/.deja/integrate_shadow/`` and for each shadow model,
prints a bucket breakdown vs production:

  - both_no_op:  both took no action
  - agree:       both acted on the same things
  - prod_extra:  production acted, shadow did nothing
  - shadow_extra:shadow acted, production didn't
  - disagree:    both acted but on different things

Record shape evolved over time — the script handles all three:
  (1) legacy pre-flip ``flash_lite`` / ``flash`` keys
  (2) post-flip single-shadow ``production`` / ``shadow`` keys
  (3) multi-shadow ``production`` + ``shadows: [...]`` list (current)

Usage:
    ./venv/bin/python tools/integrate_shadow_compare.py
    ./venv/bin/python tools/integrate_shadow_compare.py --detailed
    ./venv/bin/python tools/integrate_shadow_compare.py --since 2026-04-12T15:00
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path


def _action_summary(result: dict | None) -> dict:
    """Compact summary of a model's output: what did it propose?"""
    if result is None or result.get("error"):
        return {"available": False}

    updates = result.get("wiki_updates") or []
    goal_actions = result.get("goal_actions") or []
    tasks_update = result.get("tasks_update") or {}
    task_changes = sorted(k for k, v in tasks_update.items() if v)
    no_op = not updates and not goal_actions and not task_changes

    update_tuples = tuple(
        sorted(
            (
                u.get("action", "?"),
                f"{u.get('category','?')}/{u.get('slug','?')}",
            )
            for u in updates
        )
    )
    goal_tuples = tuple(sorted(g.get("type", "?") for g in goal_actions))
    return {
        "available": True,
        "updates": len(updates),
        "goal_actions": len(goal_actions),
        "task_changes": task_changes,
        "no_op": no_op,
        "update_tuples": update_tuples,
        "goal_tuples": goal_tuples,
    }


def _classify(prod: dict, shadow: dict) -> str:
    if not shadow.get("available"):
        return "shadow_unavailable"
    if not prod.get("available"):
        # Can't classify if prod failed; treat as N/A.
        return "shadow_unavailable"
    if prod["no_op"] and shadow["no_op"]:
        return "both_no_op"
    if prod["no_op"] and not shadow["no_op"]:
        return "shadow_extra"
    if not prod["no_op"] and shadow["no_op"]:
        return "prod_extra"
    if (
        prod["update_tuples"] == shadow["update_tuples"]
        and prod["goal_tuples"] == shadow["goal_tuples"]
        and prod["task_changes"] == shadow["task_changes"]
    ):
        return "agree"
    return "disagree"


def _shadow_list(rec: dict) -> list[dict]:
    """Return shadows as a list, handling all three record shapes."""
    shadows = rec.get("shadows")
    if isinstance(shadows, list):
        return shadows
    # Legacy single-shadow
    single = rec.get("shadow") or rec.get("flash")
    if isinstance(single, dict):
        return [single]
    return []


def _print_bucket_table(buckets: Counter, total: int) -> None:
    print(f"{'Bucket':<22}{'Count':>8}  {'%':>6}")
    print("-" * 40)
    for cls in (
        "both_no_op", "agree", "prod_extra", "shadow_extra", "disagree",
        "shadow_unavailable",
    ):
        n = buckets.get(cls, 0)
        pct = 100 * n / total if total else 0
        print(f"{cls:<22}{n:>8}  {pct:>5.1f}%")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", default=None, help="Override shadow dir")
    parser.add_argument("--since", default=None, help="YYYY-MM-DD[Thh:mm] filter")
    parser.add_argument("--detailed", action="store_true", help="Show non-agreement cases")
    args = parser.parse_args()

    shadow_dir = Path(args.dir) if args.dir else Path.home() / ".deja/integrate_shadow"
    if not shadow_dir.is_dir():
        print(f"no shadow dir at {shadow_dir}", file=sys.stderr)
        return 1

    since_dt = None
    if args.since:
        try:
            since_dt = datetime.fromisoformat(args.since)
        except ValueError:
            print(f"bad --since: {args.since}", file=sys.stderr)
            return 1

    # Per-shadow buckets, keyed by shadow model id. Production is shared.
    per_shadow_buckets: dict[str, Counter[str]] = {}
    per_shadow_latency: dict[str, list[int]] = {}
    per_shadow_details: dict[str, dict[str, list[dict]]] = {}
    prod_latency_ms: list[int] = []
    prod_models: Counter[str] = Counter()
    shadow_models: Counter[str] = Counter()

    total = 0
    # Count files where a given shadow was present, so % in per-shadow
    # reports reflects that shadow's own sample size (3.1 Pro was added
    # later than Flash-Lite).
    per_shadow_total: Counter[str] = Counter()

    files = sorted(shadow_dir.glob("*.json"))
    for fp in files:
        try:
            rec = json.loads(fp.read_text())
        except Exception:
            continue

        ts = rec.get("timestamp", "")
        if since_dt:
            try:
                if datetime.fromisoformat(ts) < since_dt:
                    continue
            except ValueError:
                continue

        prod_rec = rec.get("production") or rec.get("flash_lite")
        shadows = _shadow_list(rec)
        if not prod_rec or not shadows:
            continue

        total += 1
        prod = _action_summary(prod_rec)
        if prod_rec.get("model"):
            prod_models[prod_rec["model"]] += 1
        p_lat = prod_rec.get("latency_ms")
        if isinstance(p_lat, int):
            prod_latency_ms.append(p_lat)

        for shadow_rec in shadows:
            model = shadow_rec.get("model") or "unknown"
            shadow_models[model] += 1
            per_shadow_total[model] += 1
            summary = _action_summary(shadow_rec)
            cls = _classify(prod, summary)
            per_shadow_buckets.setdefault(model, Counter())[cls] += 1
            s_lat = shadow_rec.get("latency_ms")
            if isinstance(s_lat, int):
                per_shadow_latency.setdefault(model, []).append(s_lat)

            if cls not in ("both_no_op", "agree"):
                per_shadow_details.setdefault(model, {}).setdefault(cls, []).append({
                    "file": fp.name,
                    "timestamp": ts,
                    "prod_reasoning": (prod_rec or {}).get("reasoning", "")[:240],
                    "shadow_reasoning": (shadow_rec or {}).get("reasoning", "")[:240],
                    "signals_preview": rec.get("signals_text", "")[:200],
                })

    print(f"\nIntegrate shadow comparison — {total} cycles analyzed")
    if total == 0:
        print("(no cycles yet — run Deja for a while with the flag on)")
        return 0

    if prod_models:
        print(f"Production model(s): {dict(prod_models)}")
    if shadow_models:
        print(f"Shadow model(s):     {dict(shadow_models)}")

    if len(prod_models) > 1:
        print()
        print("⚠️  WARNING: multiple production models in this window.")
        print("    Slice by flip time for clean data, e.g.:")
        print("      --since 2026-04-12T15:00")

    if prod_latency_ms:
        prod_latency_ms.sort()
        print()
        print(f"Production latency: median {prod_latency_ms[len(prod_latency_ms)//2]}ms, "
              f"p95 {prod_latency_ms[int(len(prod_latency_ms)*0.95)]}ms")

    for model in sorted(per_shadow_buckets.keys()):
        n = per_shadow_total[model]
        print()
        print(f"=== vs {model} ({n} cycles) ===")
        _print_bucket_table(per_shadow_buckets[model], n)
        lat = per_shadow_latency.get(model, [])
        if lat:
            lat.sort()
            print(f"Shadow latency:     median {lat[len(lat)//2]}ms, "
                  f"p95 {lat[int(len(lat)*0.95)]}ms")

        buckets = per_shadow_buckets[model]
        prod_extra = buckets.get("prod_extra", 0)
        shadow_extra = buckets.get("shadow_extra", 0)
        disagree = buckets.get("disagree", 0)
        total_acted = n - buckets.get("both_no_op", 0) - buckets.get("shadow_unavailable", 0)
        print("Headline:")
        if total_acted == 0:
            print("  Nothing non-trivial — too quiet a period to judge.")
        elif prod_extra > shadow_extra * 1.5 and prod_extra >= 3:
            print(f"  Production acted alone {prod_extra}× vs shadow's {shadow_extra}×.")
            print(f"  → Prod is more aggressive than {model}.")
        elif shadow_extra > prod_extra * 1.5 and shadow_extra >= 3:
            print(f"  {model} acted alone {shadow_extra}× vs prod's {prod_extra}×.")
            print(f"  → {model} catches things prod misses.")
        else:
            print(f"  Largely agree on WHEN ({prod_extra} prod-only, {shadow_extra} shadow-only).")
            print(f"  Disagreements about WHAT: {disagree}.")

    if args.detailed:
        for model, details in per_shadow_details.items():
            for cls, items in details.items():
                print(f"\n--- {model} {cls.upper()} ({len(items)}) ---")
                for it in items[:10]:
                    print(f"\n  [{it['timestamp']}] {it['file']}")
                    print(f"  Signals: {it['signals_preview'][:160]!r}")
                    print(f"  PROD:   {it['prod_reasoning'][:200]}")
                    print(f"  SHADOW: {it['shadow_reasoning'][:200]}")
                if len(items) > 10:
                    print(f"  ... {len(items) - 10} more")

    print(f"\nShadow files at: {shadow_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
