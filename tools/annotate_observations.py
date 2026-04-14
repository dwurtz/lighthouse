"""Walk recent observations and annotate each as correct / incorrect / partial.

Writes annotations to ``~/.deja/observation_annotations.jsonl`` keyed by the
observation's ``id_key``. Resumable — re-running skips already-annotated
entries unless ``--redo`` is passed.

Purpose: build a labeled dataset of signal quality so we can reason about
where the agent's wrong views of the world come from (OCR misreads? wrong
sender attribution? misclassified source?) and prioritize fixes.

Usage:
    ./venv/bin/python tools/annotate_observations.py
    ./venv/bin/python tools/annotate_observations.py --source screenshot --since 2026-04-13
    ./venv/bin/python tools/annotate_observations.py --stats
    ./venv/bin/python tools/annotate_observations.py --source clipboard --limit 50

Controls (per observation):
    y = correct / accurate signal
    n = incorrect (wrong OCR, wrong sender, misclassified, hallucinated)
    p = partially correct (e.g. right content, wrong attribution)
    s = skip (don't annotate, don't ask again in this session)
    u = undo previous
    q = save and quit
    Any non-empty text you type before pressing enter is saved as a note.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

OBS_PATH = Path.home() / ".deja" / "observations.jsonl"
ANN_PATH = Path.home() / ".deja" / "observation_annotations.jsonl"


VERDICTS = {
    "y": "correct",
    "n": "incorrect",
    "p": "partial",
}


def _load_annotations() -> dict[str, dict]:
    """Return the latest annotation per id_key."""
    if not ANN_PATH.exists():
        return {}
    out: dict[str, dict] = {}
    for line in ANN_PATH.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        key = rec.get("id_key")
        if key:
            out[key] = rec
    return out


def _append_annotation(rec: dict) -> None:
    with ANN_PATH.open("a") as f:
        f.write(json.dumps(rec) + "\n")


def _load_observations(
    since: datetime | None,
    source: str | None,
    limit: int | None,
    text_match: str | None,
) -> list[dict]:
    """Filtered list of observations, newest first."""
    rows: list[dict] = []
    for line in OBS_PATH.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue

        ts = rec.get("timestamp", "")
        if since:
            try:
                if datetime.fromisoformat(ts) < since:
                    continue
            except ValueError:
                continue

        if source and rec.get("source") != source:
            continue
        if text_match and text_match.lower() not in (rec.get("text") or "").lower():
            continue

        rows.append(rec)

    rows.sort(key=lambda r: r.get("timestamp", ""), reverse=True)
    if limit:
        rows = rows[:limit]
    return rows


def _render_obs(rec: dict, idx: int, total: int, n_text: int = 800) -> str:
    text = rec.get("text") or ""
    if len(text) > n_text:
        text = text[:n_text] + f"\n… [+{len(rec['text']) - n_text} more chars]"
    return (
        f"\n{'=' * 72}\n"
        f"[{idx + 1}/{total}]  {rec.get('timestamp', '?')}  "
        f"source={rec.get('source', '?')}  sender={rec.get('sender', '?')}  "
        f"id={rec.get('id_key', '?')}\n"
        f"{'-' * 72}\n"
        f"{text}\n"
        f"{'=' * 72}"
    )


def _stats_mode() -> int:
    """Show annotation totals by source + verdict."""
    ann = _load_annotations()
    if not ann:
        print("No annotations yet. Run without --stats to start.")
        return 0

    by_source_verdict: dict[tuple[str, str], int] = {}
    for rec in ann.values():
        key = (rec.get("source", "?"), rec.get("verdict", "?"))
        by_source_verdict[key] = by_source_verdict.get(key, 0) + 1

    sources = sorted({s for s, _ in by_source_verdict.keys()})
    verdicts = ["correct", "partial", "incorrect"]

    print(f"\nAnnotations: {len(ann)} observations reviewed\n")
    print(f"{'source':<14}" + "".join(f"{v:>12}" for v in verdicts) + f"{'total':>8}{'% bad':>8}")
    print("-" * (14 + 12 * 3 + 16))
    total_bad = 0
    total_all = 0
    for s in sources:
        counts = [by_source_verdict.get((s, v), 0) for v in verdicts]
        total = sum(counts)
        bad = counts[1] + counts[2]  # partial + incorrect
        pct = 100 * bad / total if total else 0
        print(f"{s:<14}" + "".join(f"{c:>12}" for c in counts) + f"{total:>8}{pct:>7.0f}%")
        total_bad += bad
        total_all += total
    print("-" * (14 + 12 * 3 + 16))
    pct = 100 * total_bad / total_all if total_all else 0
    print(f"{'TOTAL':<14}" + " " * (12 * 3) + f"{total_all:>8}{pct:>7.0f}%")

    # Show a sample of bad ones per source
    bad_by_source: dict[str, list[dict]] = {}
    for rec in ann.values():
        if rec.get("verdict") in ("incorrect", "partial"):
            bad_by_source.setdefault(rec.get("source", "?"), []).append(rec)

    for s, items in bad_by_source.items():
        if not items:
            continue
        print(f"\n--- {s}: {len(items)} bad ---")
        for it in items[:5]:
            note = it.get("note") or "(no note)"
            print(f"  [{it['verdict']:<9}] {it.get('text_preview', '')[:90]}  — {note[:80]}")

    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--since", help="ISO date/time; only observations >= this")
    p.add_argument("--source", help="filter: screenshot, browser, email, etc.")
    p.add_argument("--limit", type=int, help="max observations to review")
    p.add_argument("--match", help="substring match against text (case-insensitive)")
    p.add_argument("--redo", action="store_true", help="re-review already-annotated")
    p.add_argument("--stats", action="store_true", help="print summary of existing annotations")
    args = p.parse_args()

    if args.stats:
        return _stats_mode()

    if not OBS_PATH.exists():
        print(f"no observations at {OBS_PATH}", file=sys.stderr)
        return 1

    since_dt = None
    if args.since:
        try:
            since_dt = datetime.fromisoformat(args.since)
        except ValueError:
            print(f"bad --since: {args.since}", file=sys.stderr)
            return 1

    rows = _load_observations(since_dt, args.source, args.limit, args.match)
    ann = _load_annotations()

    if not args.redo:
        rows = [r for r in rows if r.get("id_key") not in ann]

    if not rows:
        print("Nothing to review. Already annotated, or filter matched nothing.")
        return 0

    print(f"\n{len(rows)} observations to review.")
    print("Keys: [y]correct  [n]incorrect  [p]partial  [s]skip  [u]undo  [q]save+quit")
    print("Type a note before pressing the key letter — it's saved alongside the verdict.\n")

    last_written: str | None = None
    i = 0
    while i < len(rows):
        rec = rows[i]
        print(_render_obs(rec, i, len(rows)))
        try:
            raw = input("verdict (or note+verdict): ").strip()
        except EOFError:
            print()
            break
        except KeyboardInterrupt:
            print()
            break

        if not raw:
            continue

        # Allow "note text y" or "y" or just "y some note". Last token must be
        # a verdict letter; everything before is the note.
        parts = raw.rsplit(maxsplit=1)
        if len(parts) == 2 and parts[1] in ("y", "n", "p", "s", "u", "q"):
            note, key = parts
        elif raw in ("y", "n", "p", "s", "u", "q"):
            note, key = "", raw
        else:
            # Single unknown token; treat the trailing char as the verdict
            # iff it's a verdict key.
            if raw[-1] in ("y", "n", "p", "s", "u", "q"):
                note, key = raw[:-1].strip(), raw[-1]
            else:
                print(f"unrecognized: {raw!r}. Use y/n/p/s/u/q.")
                continue

        if key == "q":
            break
        if key == "s":
            i += 1
            continue
        if key == "u":
            if last_written is None:
                print("(nothing to undo)")
                continue
            print(f"undoing: {last_written}  (annotation still in file, but will be overwritten on next save)")
            i = max(i - 1, 0)
            last_written = None
            continue

        verdict = VERDICTS[key]
        entry = {
            "id_key": rec.get("id_key"),
            "source": rec.get("source"),
            "sender": rec.get("sender"),
            "timestamp": rec.get("timestamp"),
            "verdict": verdict,
            "note": note,
            "text_preview": (rec.get("text") or "")[:200],
            "annotated_at": datetime.now().isoformat(),
        }
        _append_annotation(entry)
        last_written = f"{verdict}: {note[:40]}"
        i += 1

    # Brief post-summary
    print("\n--- session summary ---")
    _stats_mode()
    return 0


if __name__ == "__main__":
    sys.exit(main())
