"""3-way comparison for the screenshot preprocess decision:

  (a) cloud_desc        — Gemini vision on the raw image (quality ceiling)
  (b) RAW OCR           — what integrate sees today when preprocess is
                          OFF (the post-graphiti-revert baseline)
  (c) PREPROCESS output — what integrate will see once preprocess is
                          wired back in (the live ``_SYSTEM_PROMPT``
                          from ``deja.screenshot_preprocess``)

Tells us two things side-by-side:
  • Does PREPROCESS recover the signal cloud_desc captured? (ceiling check)
  • Does PREPROCESS beat RAW OCR enough to justify the per-screenshot
    gpt-4.1-mini call? (real baseline)

Output: /tmp/preprocess_compare.md — scan top-to-bottom. Makes NO
changes to the live pipeline — read-only eval.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from deja.screenshot_preprocess import _SYSTEM_PROMPT as PREPROCESS_PROMPT  # noqa: E402
from deja.screenshot_preprocess import _get_api_key  # noqa: E402


DEJA_OCR = "/Applications/Deja.app/Contents/MacOS/deja-ocr"
SHADOW_DIR = Path.home() / ".deja" / "vision_shadow"


async def run_preprocess(client, user_message: str) -> str:
    resp = await client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=[
            {"role": "system", "content": PREPROCESS_PROMPT},
            {"role": "user", "content": user_message},
        ],
        max_tokens=1500,
        temperature=0.0,
    )
    return (resp.choices[0].message.content or "").strip()


def ocr_image(path: Path) -> str:
    try:
        out = subprocess.run(
            [DEJA_OCR, str(path)], capture_output=True, text=True, timeout=15
        )
        return (out.stdout or "").strip()
    except Exception as e:
        return f"(OCR failed: {e})"


async def process_one(client, rec_json: Path) -> dict:
    img = rec_json.with_suffix(".png")
    if not img.exists():
        return {"id": rec_json.stem, "error": "no image"}
    ref = json.loads(rec_json.read_text())
    cloud_desc = (ref.get("cloud_desc") or "").strip()

    ocr_text = ocr_image(img)
    if not ocr_text or ocr_text.startswith("(OCR failed"):
        return {"id": rec_json.stem, "error": ocr_text or "empty OCR"}

    # deja-ocr returns "[Focused: app — window]\n\n<text>" when the
    # focused-frame crop fires. Extract the header so the preprocess
    # prompt sees the same app/window the live pipeline passes. If no
    # focused header is present, fall back to blank — that matches what
    # the live pipeline does when AX context is missing.
    app, title = "", ""
    first_line, _, rest = ocr_text.partition("\n")
    if first_line.startswith("[Focused:") and "—" in first_line:
        inner = first_line[9:].rstrip("]").strip()
        app_part, _, title_part = inner.partition("—")
        app = app_part.strip()
        title = title_part.strip().strip('"')
        body = rest.strip()
    else:
        body = ocr_text

    # Raw-OCR column mirrors what sig.text looks like in production today:
    # "[<label>]\n\n<ocr_text>" — the label string the live pipeline
    # builds via _label_for_app. We approximate by joining app+title.
    raw_label = f"{app}: {title}" if app or title else "screen"
    raw_signal = f"[{raw_label}]\n\n{body}"

    user_msg = f"App: {app}\nWindow: {title}\n\nOCR text:\n{body}"
    preprocess_out = await run_preprocess(client, user_msg)

    return {
        "id": rec_json.stem,
        "app": app,
        "title": title,
        "cloud_desc": cloud_desc,
        "raw_signal": raw_signal,
        "preprocess": preprocess_out,
        "raw_chars": len(raw_signal),
        "preprocess_chars": len(preprocess_out),
    }


async def main(n: int, out_path: Path) -> None:
    recs = sorted(SHADOW_DIR.glob("*.json"))
    random.seed(13)
    picks = random.sample(recs, min(n, len(recs)))
    print(f"Picked {len(picks)} shadow records", file=sys.stderr)

    key = _get_api_key()
    if not key:
        print("No OpenAI key available — abort", file=sys.stderr)
        sys.exit(1)
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=key)

    # Stream each record to the output file as soon as it completes so
    # a kill mid-run keeps what's finished.
    out_path.write_text("# screenshot preprocess 3-way eval\n\n", encoding="utf-8")

    results: list[dict] = []
    for i, p in enumerate(picks, 1):
        print(f"  [{i:02d}/{len(picks)}] {p.name}", file=sys.stderr, flush=True)
        r = await process_one(client, p)
        results.append(r)
        # Append this result to the file
        lines: list[str] = []
        header = f"## {i:02d}. `{r['id']}` ({r.get('app','?')} — {r.get('title','?')})\n"
        lines.append(header)
        if r.get("error"):
            lines.append(f"*error: {r['error']}*\n\n")
        else:
            lines.append(
                f"*raw signal: {r['raw_chars']} chars  •  "
                f"preprocess: {r['preprocess_chars']} chars  •  "
                f"reduction: {100 * (1 - r['preprocess_chars']/max(1,r['raw_chars'])):.0f}%*\n\n"
            )
            lines.append("### (a) cloud_desc — Gemini vision (reference)\n\n")
            lines.append(f"{r['cloud_desc'] or '*(none)*'}\n\n")
            lines.append("### (b) RAW OCR signal — what integrate sees today\n\n")
            lines.append("```\n" + r["raw_signal"] + "\n```\n\n")
            lines.append("### (c) PREPROCESS output — proposed\n\n")
            lines.append("```\n" + r["preprocess"] + "\n```\n\n")
            lines.append("---\n\n")
        with out_path.open("a", encoding="utf-8") as f:
            f.write("".join(lines))
    print(f"Wrote {out_path}", file=sys.stderr)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--out", type=Path, default=Path("/tmp/preprocess_compare.md"))
    args = ap.parse_args()
    asyncio.run(main(args.n, args.out))
