"""Replay recent observation windows through integrate to inspect
`observation_narrative` quality.

Picks 20 distinct 15-minute windows from ``~/.deja/observations.jsonl``,
formats each with the live ``format_signals`` (timeline + [T1]/[T2]/[T3]
markers), runs them through ``GeminiClient.integrate_observations``
(which picks up the live ``integrate.md`` prompt), and prints only the
narrative plus a one-line batch summary.

Uses today's wiki + goals + contacts as context — so narratives reflect
how Deja would interpret OLD signals given the CURRENT wiki state. Good
for quality evaluation of the narrative itself; NOT a fair test of
would-have-been wiki updates.

Usage:
    uv run python tools/narrative_replay.py
    uv run python tools/narrative_replay.py --n 10        # fewer windows
    uv run python tools/narrative_replay.py --out out.md  # write to file
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Ensure src/ is on path when invoked via `uv run python tools/...`
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from deja.config import DEJA_HOME  # noqa: E402
from deja.signals.format import format_signals  # noqa: E402


WINDOW_MIN = 15
MIN_SIGNALS = 4
ALLOWED_SOURCES = {
    "email",
    "imessage",
    "whatsapp",
    "typed",
    "voice",
    "screenshot",
    "chat",
    "clipboard",
    "browser",
}


def _parse_ts(s: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat((s or "").replace("Z", "+00:00"))
    except Exception:
        return None
    # Normalize every timestamp to UTC-aware so naive and aware don't
    # collide during sort/compare. Observations historically mix both.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def load_signals(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("source") not in ALLOWED_SOURCES:
                continue
            ts = _parse_ts(d.get("timestamp", ""))
            if ts is None:
                continue
            d["_ts"] = ts
            rows.append(d)
    rows.sort(key=lambda r: r["_ts"])
    return rows


def build_windows(rows: list[dict], target: int) -> list[list[dict]]:
    """Slide non-overlapping 15-min windows; keep those with ≥ MIN_SIGNALS."""
    windows: list[list[dict]] = []
    used_end: datetime | None = None
    i = 0
    while i < len(rows):
        start = rows[i]["_ts"]
        end = start + timedelta(minutes=WINDOW_MIN)
        if used_end and start < used_end:
            i += 1
            continue
        win: list[dict] = []
        j = i
        while j < len(rows) and rows[j]["_ts"] < end:
            win.append(rows[j])
            j += 1
        if len(win) >= MIN_SIGNALS:
            windows.append(win)
            used_end = end
        i = j if j > i else i + 1

    if len(windows) > target:
        step = max(1, len(windows) // target)
        windows = windows[::step][:target]
    return windows


def cap_screenshots(win: list[dict], max_shots: int = 6) -> list[dict]:
    structured = [r for r in win if r["source"] != "screenshot"]
    shots = [r for r in win if r["source"] == "screenshot"]
    sampled = shots[:: max(1, len(shots) // max_shots)][:max_shots]
    return sorted(structured + sampled, key=lambda r: r["_ts"])


def _strip_internal(obs: dict) -> dict:
    return {k: v for k, v in obs.items() if k != "_ts"}


def _src_counts(win: list[dict]) -> str:
    c: dict[str, int] = defaultdict(int)
    for s in win:
        c[s["source"]] += 1
    return ", ".join(f"{k}={v}" for k, v in sorted(c.items()))


async def run_one(client, batch: list[dict], wiki_text: str) -> dict:
    signals_text = format_signals(batch)
    return await client.integrate_observations(
        signals_text=signals_text,
        wiki_text=wiki_text,
        open_windows="",
    )


async def main(n: int, out_path: Path | None) -> None:
    obs_file = DEJA_HOME / "observations.jsonl"
    if not obs_file.exists():
        print(f"no observations file at {obs_file}", file=sys.stderr)
        sys.exit(1)

    print(f"Loading {obs_file}...", file=sys.stderr)
    rows = load_signals(obs_file)
    print(f"  {len(rows)} signals", file=sys.stderr)

    windows = build_windows(rows, target=n)
    windows = [cap_screenshots(w) for w in windows]
    print(f"  {len(windows)} windows selected", file=sys.stderr)

    # Load wiki context ONCE (same for every replay call).
    from deja.wiki_retriever import build_analysis_context

    print("Building wiki context (using current wiki state)...", file=sys.stderr)
    # Use the union of batches' signal ids as the retrieval seed so QMD
    # returns something reasonable. For replay purposes we pass the
    # first window — retrieval quality isn't the point of this run.
    try:
        wiki_text = build_analysis_context([_strip_internal(o) for o in windows[0]])
    except Exception:
        from deja import wiki as wiki_store
        wiki_text = wiki_store.render_for_prompt()

    # Disable integrate shadow eval for this replay — we're testing
    # prompt output, not model A/B, and shadow fires Pro 3.1 which
    # bottlenecks the proxy. Patch the lazy wrapper directly.
    from deja import config as _cfg
    _cfg.INTEGRATE_SHADOW_EVAL = False  # type: ignore[assignment]

    from deja.llm_client import GeminiClient

    client = GeminiClient()

    # Cap concurrency — the Gemini proxy is on Render Starter
    # (512 MB / 0.5 CPU) and can't handle multiple concurrent Flash
    # calls without hanging. Sequential is slower but reliable.
    sem = asyncio.Semaphore(1)
    print(f"Running {len(windows)} integrate calls (sequential)...", file=sys.stderr)

    done = 0
    lock = asyncio.Lock()
    # Stream each narrative to the output file as soon as it lands so a
    # kill mid-run keeps what's finished. Open in write mode once here
    # to truncate any stale content, then append per-result.
    if out_path:
        out_path.write_text("", encoding="utf-8")

    async def _one(idx: int, win: list[dict]):
        nonlocal done
        batch = [_strip_internal(o) for o in win]
        async with sem:
            for attempt in (1, 2):
                try:
                    result = await run_one(client, batch, wiki_text)
                    out = (idx, win, result, None)
                    break
                except Exception as e:
                    if attempt == 2:
                        out = (idx, win, None, f"{type(e).__name__}: {e}")
                        break
                    await asyncio.sleep(5)
        async with lock:
            done += 1
            status = "OK" if out[3] is None else f"err({out[3][:60]})"
            print(
                f"  [{done:02d}/{len(windows)}] W{idx:02d} {status}",
                file=sys.stderr,
                flush=True,
            )
            if out_path:
                ts_start = out[1][0]["_ts"].strftime("%Y-%m-%d %H:%M")
                ts_end = out[1][-1]["_ts"].strftime("%H:%M")
                header = f"## W{out[0]:02d} — {ts_start} → {ts_end} (n={len(out[1])}, {_src_counts(out[1])})"
                if out[3]:
                    body = f"*error: {out[3]}*"
                else:
                    narr = (out[2].get("observation_narrative") or "").strip()
                    wu = len(out[2].get("wiki_updates") or [])
                    body = narr or "*(empty narrative)*"
                    body += f"\n\n*wiki_updates emitted: {wu}*"
                with out_path.open("a", encoding="utf-8") as f:
                    f.write(f"{header}\n\n{body}\n\n")
        return out

    results = await asyncio.gather(*(_one(i, w) for i, w in enumerate(windows, 1)))

    lines: list[str] = []
    for idx, win, result, err in results:
        ts_start = win[0]["_ts"].strftime("%Y-%m-%d %H:%M")
        ts_end = win[-1]["_ts"].strftime("%H:%M")
        header = f"## W{idx:02d} — {ts_start} → {ts_end} (n={len(win)}, {_src_counts(win)})"
        if err:
            body = f"*error: {err}*"
        else:
            narr = (result.get("observation_narrative") or "").strip()
            wu = len(result.get("wiki_updates") or [])
            body = narr or "*(empty narrative)*"
            body += f"\n\n*wiki_updates emitted: {wu}*"
        lines.append(f"{header}\n\n{body}\n")

    report = "\n".join(lines)
    if out_path:
        # File was written incrementally (out of order). Rewrite now in
        # window-index order so it's easy to read top-to-bottom.
        out_path.write_text(report, encoding="utf-8")
        print(f"\nWrote {out_path} (reordered)", file=sys.stderr)
    print(report)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()
    asyncio.run(main(args.n, args.out))
