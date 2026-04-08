"""Vision model A/B evaluation harness.

Runs every real-usage fixture in ``tests/fixtures/screenshots/`` through N
Gemini models, computes objective per-frame metrics, and reports aggregate
win rates and cost so we can decide whether a pricier vision model is
worth the delta for deja specifically.

Objective metrics (no judge model needed):
  - length_chars           — depth of description
  - entity_count           — rough "how many specific nouns" via a regex
                             (proper nouns, filenames, URLs, quoted strings)
  - wiki_link_count        — number of [[slug]] wiki-links emitted
  - must_contain_hits      — fraction of spec.yaml must_contain strings matched
  - must_not_contain_hits  — hallucination count (lower is better)
  - has_sent_marker        — '[SENT]' present when the frame is a messaging app
  - cost_cents             — estimated from SDK usage_metadata * model rates

Usage:
    ./venv/bin/python tools/vision_eval.py \\
        --models flash-lite,flash,pro \\
        --samples 2 \\
        --fixtures tests/fixtures/screenshots/

Saves results to ``~/.deja/vision_eval/<timestamp>.jsonl`` so runs
can be compared over time. Prints a summary table at the end.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

import yaml

# Ensure ``deja`` is importable when run from the repo root.
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))

from deja.config import WIKI_DIR, DEJA_HOME  # noqa: E402
from deja.prompts import load as load_prompt  # noqa: E402


# ---------------------------------------------------------------------------
# Model alias → Gemini model id and pricing (approximate, per 1M tokens)
# ---------------------------------------------------------------------------

MODELS = {
    "flash-lite": {
        "id": "gemini-2.5-flash-lite",
        "input_per_mtok_usd": 0.10,
        "output_per_mtok_usd": 0.40,
    },
    "flash": {
        "id": "gemini-2.5-flash",
        "input_per_mtok_usd": 0.30,
        "output_per_mtok_usd": 2.50,
    },
    "pro": {
        "id": "gemini-2.5-pro",
        "input_per_mtok_usd": 1.25,
        "output_per_mtok_usd": 10.00,
    },
}


# ---------------------------------------------------------------------------
# Per-sample result
# ---------------------------------------------------------------------------

@dataclass
class SampleResult:
    fixture: str
    model: str
    sample_idx: int
    summary: str
    length_chars: int
    entity_count: int
    wiki_link_count: int
    must_contain_total: int
    must_contain_hits: int
    must_not_contain_hits: int
    has_sent_marker: bool
    wiki_match_hit: bool
    input_tokens: int
    output_tokens: int
    cost_cents: float
    latency_ms: int
    error: str | None = None


# ---------------------------------------------------------------------------
# Metric helpers
# ---------------------------------------------------------------------------

_WIKILINK_RE = re.compile(r"\[\[([^\]\n|]+)(?:\|[^\]\n]*)?\]\]")
_ENTITY_RE = re.compile(
    r"(?:"
    r"[A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+){1,3}"         # Proper nouns like "Amanda Peffer", "Rocket Lab"
    r"|[a-zA-Z_][\w./-]*\.(?:py|md|liquid|js|ts|json|yaml|html|css)"  # filenames
    r"|https?://\S+"                                      # URLs
    r"|\"[^\"\n]{3,80}\""                                 # quoted strings (verbatim quotes)
    r")"
)


def count_entities(text: str) -> int:
    """Rough specificity metric — counts proper nouns, filenames, URLs, quotes.

    Not perfect (false positives on sentence-starting capitalization) but
    good enough for relative A/B comparison between models on the same
    set of fixtures.
    """
    return len(_ENTITY_RE.findall(text))


def count_wiki_links(text: str) -> int:
    return len(_WIKILINK_RE.findall(text))


def has_sent_marker(text: str) -> bool:
    return "[SENT]" in text or "[sent]" in text.lower()


def check_must_contain(summary: str, case: dict) -> tuple[int, int]:
    needles = case.get("must_contain") or []
    lower = summary.lower()
    hits = sum(1 for n in needles if n.lower() in lower)
    return hits, len(needles)


def check_must_not_contain(summary: str, case: dict) -> int:
    bads = case.get("must_not_contain") or []
    lower = summary.lower()
    return sum(1 for b in bads if b.lower() in lower)


def check_wiki_match(summary: str, case: dict) -> bool:
    wm = case.get("wiki_match")
    if not wm:
        return True  # no expectation → trivially satisfied
    lower = summary.lower()
    link_form = f"[[{wm}]]".lower()
    prose_form = wm.replace("-", " ").lower()
    return link_form in lower or prose_form in lower


# ---------------------------------------------------------------------------
# One vision call through one model
# ---------------------------------------------------------------------------

async def describe_one(
    client,
    types_mod,
    image_bytes: bytes,
    mime: str,
    prompt: str,
    model_id: str,
) -> tuple[str, int, int, int]:
    """Call one vision model once. Returns (text, in_tokens, out_tokens, latency_ms).

    Gemini 2.5 Pro burns hidden thinking tokens against ``max_output_tokens``
    and returns an empty candidate when the budget is exhausted, so we use
    a generous cap (2048) and explicitly set ``thinking_config.thinking_budget=0``
    for Pro so it skips the thinking pass entirely — a visual description is
    not a task that benefits from chain-of-thought.
    """
    # Gemini 2.5 Pro burns hidden thinking tokens against ``max_output_tokens``
    # and the API refuses ``thinking_budget=0`` (it requires thinking mode).
    # Give Pro a generous 4096 budget so thinking + response both fit;
    # Flash and Flash-Lite only use the budget for response text, so 2048
    # is plenty for them.
    is_pro = "pro" in model_id
    config = types_mod.GenerateContentConfig(
        max_output_tokens=4096 if is_pro else 2048,
        temperature=0.2,
    )

    t0 = time.time()
    resp = await client.aio.models.generate_content(
        model=model_id,
        contents=[
            types_mod.Part.from_bytes(data=image_bytes, mime_type=mime),
            prompt,
        ],
        config=config,
    )
    latency_ms = int((time.time() - t0) * 1000)
    text = (resp.text or "").strip()
    in_tok = getattr(resp.usage_metadata, "prompt_token_count", 0) or 0
    out_tok = getattr(resp.usage_metadata, "candidates_token_count", 0) or 0
    return text, in_tok, out_tok, latency_ms


def compute_cost_cents(model_alias: str, in_tok: int, out_tok: int) -> float:
    rates = MODELS[model_alias]
    in_cost = in_tok / 1_000_000 * rates["input_per_mtok_usd"]
    out_cost = out_tok / 1_000_000 * rates["output_per_mtok_usd"]
    return round((in_cost + out_cost) * 100, 4)  # cents


def load_image_bytes(path: Path) -> tuple[bytes, str]:
    """Resize to 800px wide JPEG q75 (same as the live describe_screen)."""
    try:
        from PIL import Image
        img = Image.open(path).convert("RGB")
        if img.width > 800:
            ratio = 800 / img.width
            img = img.resize((800, int(img.height * ratio)), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=75)
        return buf.getvalue(), "image/jpeg"
    except Exception:
        return path.read_bytes(), "image/png"


def render_prompt(index_md: str) -> str:
    """Load the describe_screen prompt with real wiki grounding."""
    from deja.identity import load_user
    user_fields = load_user().as_prompt_fields()
    template = load_prompt("describe_screen")
    try:
        return template.format(index_md=index_md, **user_fields)
    except KeyError:
        return template


# ---------------------------------------------------------------------------
# Main eval loop
# ---------------------------------------------------------------------------

async def evaluate(args):
    from google import genai
    from google.genai import types as genai_types

    client = genai.Client()

    fixtures_dir = Path(args.fixtures)
    spec_path = fixtures_dir / "spec.yaml"
    spec = yaml.safe_load(spec_path.read_text()) if spec_path.exists() else {}
    cases = {c["file"]: c for c in (spec.get("cases") or []) if c.get("file")}

    # Load the live wiki index.md as the grounding context
    index_path = WIKI_DIR / "index.md"
    index_md = index_path.read_text() if index_path.exists() else ""
    prompt = render_prompt(index_md)

    model_aliases = [m.strip() for m in args.models.split(",") if m.strip()]
    for m in model_aliases:
        if m not in MODELS:
            print(f"unknown model alias: {m}. valid: {list(MODELS)}", file=sys.stderr)
            sys.exit(2)

    fixture_files = sorted(fixtures_dir.glob("*.png"))
    if not fixture_files:
        print(f"no PNG fixtures found in {fixtures_dir}", file=sys.stderr)
        sys.exit(2)

    print(f"evaluating {len(fixture_files)} fixtures × {len(model_aliases)} models × {args.samples} samples")
    print(f"models: {model_aliases}")
    print()

    results: list[SampleResult] = []

    for fx_path in fixture_files:
        fx_name = fx_path.name
        case = cases.get(fx_name) or {"file": fx_name}
        image_bytes, mime = load_image_bytes(fx_path)
        print(f"[{fx_name}]")

        for model_alias in model_aliases:
            model_id = MODELS[model_alias]["id"]
            for sample_idx in range(args.samples):
                try:
                    text, in_tok, out_tok, latency = await describe_one(
                        client, genai_types, image_bytes, mime, prompt, model_id
                    )
                    mc_hits, mc_total = check_must_contain(text, case)
                    mn_hits = check_must_not_contain(text, case)
                    wm_hit = check_wiki_match(text, case)
                    result = SampleResult(
                        fixture=fx_name,
                        model=model_alias,
                        sample_idx=sample_idx,
                        summary=text,
                        length_chars=len(text),
                        entity_count=count_entities(text),
                        wiki_link_count=count_wiki_links(text),
                        must_contain_total=mc_total,
                        must_contain_hits=mc_hits,
                        must_not_contain_hits=mn_hits,
                        has_sent_marker=has_sent_marker(text),
                        wiki_match_hit=wm_hit,
                        input_tokens=in_tok,
                        output_tokens=out_tok,
                        cost_cents=compute_cost_cents(model_alias, in_tok, out_tok),
                        latency_ms=latency,
                    )
                    results.append(result)
                    print(
                        f"  {model_alias:10s} s{sample_idx}  "
                        f"{result.entity_count:2d} ent  "
                        f"{result.wiki_link_count:2d} link  "
                        f"{mc_hits}/{mc_total} must  "
                        f"{mn_hits} bad  "
                        f"wm={'Y' if wm_hit else 'N'}  "
                        f"{result.length_chars:4d}ch  "
                        f"{result.cost_cents:.4f}¢  "
                        f"{latency}ms"
                    )
                except Exception as e:
                    print(f"  {model_alias:10s} s{sample_idx}  ERROR: {e}")
                    results.append(SampleResult(
                        fixture=fx_name,
                        model=model_alias,
                        sample_idx=sample_idx,
                        summary="",
                        length_chars=0,
                        entity_count=0,
                        wiki_link_count=0,
                        must_contain_total=0,
                        must_contain_hits=0,
                        must_not_contain_hits=0,
                        has_sent_marker=False,
                        wiki_match_hit=False,
                        input_tokens=0,
                        output_tokens=0,
                        cost_cents=0.0,
                        latency_ms=0,
                        error=str(e),
                    ))
        print()

    # Aggregate per-model summary
    print("=" * 80)
    print("AGGREGATE")
    print("=" * 80)
    header = f"{'model':12s} {'entities':>10s} {'links':>8s} {'must-hit %':>12s} {'hallu':>7s} {'len':>6s} {'cost/frame':>12s} {'latency':>10s}"
    print(header)
    print("-" * len(header))
    for m in model_aliases:
        rows = [r for r in results if r.model == m and r.error is None]
        if not rows:
            print(f"{m:12s}  (all failed)")
            continue
        avg_ent = sum(r.entity_count for r in rows) / len(rows)
        avg_link = sum(r.wiki_link_count for r in rows) / len(rows)
        avg_len = sum(r.length_chars for r in rows) / len(rows)
        avg_cost = sum(r.cost_cents for r in rows) / len(rows)
        avg_latency = sum(r.latency_ms for r in rows) / len(rows)
        must_total = sum(r.must_contain_total for r in rows)
        must_hit = sum(r.must_contain_hits for r in rows)
        must_pct = (100 * must_hit / must_total) if must_total else 0.0
        hallu = sum(r.must_not_contain_hits for r in rows)
        print(
            f"{m:12s} {avg_ent:10.1f} {avg_link:8.1f} {must_pct:11.1f}% {hallu:7d} {avg_len:6.0f} {avg_cost:11.4f}¢ {avg_latency:9.0f}ms"
        )

    # Per-model wiki_match hit rate
    print()
    print("Wiki-match hit rate (fixtures with a wiki_match expectation):")
    wm_fixtures = [c for c in cases.values() if c.get("wiki_match")]
    for m in model_aliases:
        rows = [r for r in results if r.model == m and r.error is None and cases.get(r.fixture, {}).get("wiki_match")]
        if not rows:
            continue
        hit = sum(1 for r in rows if r.wiki_match_hit)
        print(f"  {m:10s}  {hit:2d}/{len(rows):2d}  ({100 * hit / len(rows):.0f}%)")

    # Per-fixture winner (highest entity count + links, tiebreak must_contain)
    print()
    print("Per-fixture winner (entity count + wiki links):")
    by_fixture: dict[str, list[SampleResult]] = {}
    for r in results:
        if r.error:
            continue
        by_fixture.setdefault(r.fixture, []).append(r)
    model_wins: dict[str, int] = {m: 0 for m in model_aliases}
    for fx, rows in by_fixture.items():
        # Average per model for this fixture
        by_model: dict[str, list[SampleResult]] = {}
        for r in rows:
            by_model.setdefault(r.model, []).append(r)
        scored = []
        for m, rs in by_model.items():
            score = (
                sum(r.entity_count for r in rs) / len(rs)
                + 2 * sum(r.wiki_link_count for r in rs) / len(rs)
                + sum(r.must_contain_hits for r in rs) / len(rs)
            )
            scored.append((score, m))
        scored.sort(reverse=True)
        winner = scored[0][1]
        model_wins[winner] += 1
        print(f"  {fx:50s} → {winner}")
    print()
    print("Overall wins:", ", ".join(f"{m}={n}" for m, n in model_wins.items()))

    # Persist full detail for later comparison
    out_dir = DEJA_HOME / "vision_eval"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}.jsonl"
    with open(out_file, "w") as f:
        for r in results:
            f.write(json.dumps(asdict(r)) + "\n")
    print()
    print(f"full detail saved to {out_file}")


def main():
    parser = argparse.ArgumentParser(description="A/B vision model eval on real fixtures")
    parser.add_argument(
        "--models",
        default="flash-lite,flash,pro",
        help="comma-separated list of model aliases (see MODELS constant)",
    )
    parser.add_argument(
        "--samples",
        type=int,
        default=2,
        help="samples per fixture per model (averaged for stability at temp 0.2)",
    )
    parser.add_argument(
        "--fixtures",
        default=str(_REPO / "tests" / "fixtures" / "screenshots"),
    )
    args = parser.parse_args()
    asyncio.run(evaluate(args))


if __name__ == "__main__":
    main()
