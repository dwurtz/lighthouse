"""Integrate MVP: can Flash-Lite do dedup + cross-link + frontmatter?

Runs three prompt variants (control, +dedup, +dedup/crosslink/frontmatter)
against gemini-2.5-flash-lite with a synthetic fixture built around the
cruz / cruz-wurtz duplicate pair. 3 runs per variant for noise. Prints
a headline table and saves a markdown report.

Does NOT touch production files — loads prompts directly from
src/deja/prompts/integrate_v{0,1,2}_*.md and builds its own fixture.

Usage:
    ./venv/bin/python tools/integrate_mvp.py
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))

PROMPT_DIR = _REPO / "src" / "deja" / "prompts"
WIKI = Path.home() / "Deja"
REPORT_DIR = _REPO / "docs"

MODEL = "gemini-2.5-flash-lite"
TEMPERATURE = 0.2
RUNS_PER_VARIANT = 3

# Flash-Lite pricing (text): $0.10 / 1M input, $0.40 / 1M output (USD)
PRICE_IN = 0.10 / 1_000_000
PRICE_OUT = 0.40 / 1_000_000


VARIANTS = [
    ("V0_control", "integrate_v0_control.md"),
    ("V1_dedup", "integrate_v1_dedup.md"),
    ("V2_dedup_crosslink_fm", "integrate_v2_dedup_crosslink_frontmatter.md"),
]


def build_fixture() -> dict:
    """Build the synthetic test fixture around the cruz duplicate pair."""
    from deja.identity import load_user
    from deja.wiki_schema import load_schema

    user_fields = load_user().as_prompt_fields()
    schema = load_schema()

    index_head = "\n".join((WIKI / "index.md").read_text().splitlines()[:20])
    cruz_body = (WIKI / "people" / "cruz.md").read_text()
    cruz_wurtz_body = (WIKI / "people" / "cruz-wurtz.md").read_text()
    david_body = (WIKI / "people" / "david-wurtz.md").read_text()

    wiki_text = (
        "## index.md (head)\n"
        f"{index_head}\n\n"
        "## people/cruz.md\n"
        f"{cruz_body}\n\n"
        "## people/cruz-wurtz.md\n"
        f"{cruz_wurtz_body}\n\n"
        "## people/david-wurtz.md\n"
        f"{david_body}\n"
    )

    signals_text = (
        "[2026-04-10 16:42] email Lillian Diaz (lillian.diaz@isaz.edu) -> David Wurtz: "
        '"Hi David, I dropped off the math workbook for Cruz today. He was really '
        'engaged during our session — see you next week!"\n'
        "[2026-04-10 16:45] iMessage You -> Nie: "
        '"Lili dropped off the new workbook for Cruz 👍"\n'
        "[2026-04-10 17:10] calendar event accepted: "
        '"Cruz math tutoring — Tue 4pm (Lillian Diaz)"\n'
    )

    now = datetime.now()
    return {
        "current_time": now.strftime("%Y-%m-%d %H:%M"),
        "day_of_week": now.strftime("%A"),
        "time_of_day": "afternoon",
        "contacts_text": "Lillian Diaz <lillian.diaz@isaz.edu>; Dominique Wurtz (Nie); Cruz Wurtz",
        "schema": schema,
        "wiki_text": wiki_text,
        "signals_text": signals_text,
        **user_fields,
    }


@dataclass
class RunResult:
    variant: str
    run_idx: int
    ok: bool = False
    error: str = ""
    latency_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    parsed: dict = field(default_factory=dict)
    raw: str = ""
    # Scored fields
    dedup_success: bool = False
    canonical_slug: str = ""
    deleted_slug: str = ""
    crosslink_count: int = 0
    frontmatter_present: bool = False
    reason_sensible: bool = False


def load_prompt(name: str) -> str:
    return (PROMPT_DIR / name).read_text()


async def run_variant(
    variant_name: str,
    prompt_file: str,
    fixture: dict,
    run_idx: int,
) -> RunResult:
    """Load prompt, format, call Gemini, parse, return result."""
    from deja.llm_client import GeminiClient, _parse_json

    template = load_prompt(prompt_file)
    prompt_text = template.format(**fixture)

    client = GeminiClient()
    t0 = time.time()
    try:
        resp = await client._generate_full(
            model=MODEL,
            contents=prompt_text,
            config_dict={
                "response_mime_type": "application/json",
                "max_output_tokens": 16384,
                "temperature": TEMPERATURE,
            },
        )
    except Exception as e:
        # One retry on any failure (covers rate limits)
        await asyncio.sleep(10)
        try:
            resp = await client._generate_full(
                model=MODEL,
                contents=prompt_text,
                config_dict={
                    "response_mime_type": "application/json",
                    "max_output_tokens": 16384,
                    "temperature": TEMPERATURE,
                },
            )
        except Exception as e2:
            return RunResult(
                variant=variant_name,
                run_idx=run_idx,
                ok=False,
                error=f"{type(e2).__name__}: {e2}",
                latency_ms=int((time.time() - t0) * 1000),
            )

    latency_ms = int((time.time() - t0) * 1000)

    # Proxy-mode: dict with text/parts/usage_metadata.
    # Direct-mode: google-genai Response object.
    if isinstance(resp, dict):
        raw = resp.get("text", "") or ""
        usage = resp.get("usage_metadata") or {}
        in_tok = usage.get("prompt_token_count") or 0
        out_tok = usage.get("candidates_token_count") or 0
    else:
        raw = getattr(resp, "text", "") or ""
        usage = getattr(resp, "usage_metadata", None)
        in_tok = getattr(usage, "prompt_token_count", 0) or 0
        out_tok = getattr(usage, "candidates_token_count", 0) or 0

    try:
        parsed = json.loads(raw)
    except Exception:
        try:
            parsed = _parse_json(raw)
        except Exception:
            parsed = {}

    cost = in_tok * PRICE_IN + out_tok * PRICE_OUT

    result = RunResult(
        variant=variant_name,
        run_idx=run_idx,
        ok=bool(parsed),
        latency_ms=latency_ms,
        input_tokens=in_tok,
        output_tokens=out_tok,
        cost_usd=cost,
        parsed=parsed if isinstance(parsed, dict) else {},
        raw=raw,
    )
    score(result)
    return result


def score(r: RunResult) -> None:
    """Populate the scoring fields on a RunResult in place."""
    updates = r.parsed.get("wiki_updates") or []
    if not isinstance(updates, list):
        return

    cruz_slugs = {"cruz", "people/cruz"}
    cruz_wurtz_slugs = {"cruz-wurtz", "people/cruz-wurtz"}

    def _slug_is(u: dict, targets: set[str]) -> bool:
        s = (u.get("slug") or "").strip().lower()
        if s in targets:
            return True
        cat = (u.get("category") or "").strip().lower()
        combined = f"{cat}/{s}".lower()
        return combined in targets

    deletes = [u for u in updates if isinstance(u, dict) and (u.get("action") or "").lower() == "delete"]
    writes = [u for u in updates if isinstance(u, dict) and (u.get("action") or "").lower() in ("update", "create")]

    deleted_cruz = [d for d in deletes if _slug_is(d, cruz_slugs)]
    deleted_cruz_wurtz = [d for d in deletes if _slug_is(d, cruz_wurtz_slugs)]
    updated_cruz = [u for u in writes if _slug_is(u, cruz_slugs)]
    updated_cruz_wurtz = [u for u in writes if _slug_is(u, cruz_wurtz_slugs)]

    # Dedup success: exactly one of the two is updated, the OTHER is deleted.
    dedup_pairs = [
        (updated_cruz, deleted_cruz_wurtz, "cruz"),
        (updated_cruz_wurtz, deleted_cruz, "cruz-wurtz"),
    ]
    for upd, dels, canonical in dedup_pairs:
        if upd and dels:
            r.dedup_success = True
            r.canonical_slug = canonical
            r.deleted_slug = "cruz-wurtz" if canonical == "cruz" else "cruz"
            reason = (dels[0].get("reason") or "").lower()
            r.reason_sensible = "duplicate" in reason
            canonical_content = upd[0].get("content") or ""
            r.crosslink_count = len(re.findall(r"\[\[[^\]]+\]\]", canonical_content))
            r.frontmatter_present = canonical_content.lstrip().startswith("---")
            return

    # No dedup — compute overall side-signals over all writes so we can still
    # see whether cross-link / frontmatter instructions had any effect.
    all_content = "\n".join((u.get("content") or "") for u in writes)
    r.crosslink_count = len(re.findall(r"\[\[[^\]]+\]\]", all_content))
    r.frontmatter_present = any(
        (u.get("content") or "").lstrip().startswith("---") for u in writes
    )


async def main() -> None:
    print(f"Integrate MVP — model={MODEL}, {RUNS_PER_VARIANT} runs/variant")
    print(f"Prompt dir: {PROMPT_DIR}")
    fixture = build_fixture()
    print(f"Fixture wiki_text: {len(fixture['wiki_text'])} chars")
    print(f"Fixture signals_text: {len(fixture['signals_text'])} chars")
    print()

    all_results: list[RunResult] = []
    for variant_name, prompt_file in VARIANTS:
        print(f"--- {variant_name} ({prompt_file}) ---")
        for i in range(1, RUNS_PER_VARIANT + 1):
            r = await run_variant(variant_name, prompt_file, fixture, i)
            all_results.append(r)
            if not r.ok:
                print(f"  run {i}: ERROR {r.error}")
                continue
            print(
                f"  run {i}: dedup={'Y' if r.dedup_success else 'N'} "
                f"canonical={r.canonical_slug or '-':10s} "
                f"links={r.crosslink_count:2d} "
                f"fm={'Y' if r.frontmatter_present else 'N'} "
                f"reason_ok={'Y' if r.reason_sensible else 'N'} "
                f"tok={r.input_tokens}/{r.output_tokens} "
                f"${r.cost_usd:.5f} "
                f"{r.latency_ms/1000:.1f}s"
            )
        print()

    # Aggregate
    total_cost = sum(r.cost_usd for r in all_results)
    print(f"Total cost: ${total_cost:.4f}")
    print()

    # Build report
    report = build_report(all_results, fixture, total_cost)
    print(report)

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    iso = datetime.now().strftime("%Y-%m-%dT%H%M")
    report_path = REPORT_DIR / f"integrate-mvp-flashlite-{iso}.md"
    report_path.write_text(report)
    print(f"\nFull report saved to: {report_path}")


def build_report(results: list[RunResult], fixture: dict, total_cost: float) -> str:
    lines: list[str] = []
    lines.append(f"# Integrate MVP — Flash-Lite capability test ({datetime.now().isoformat(timespec='seconds')})")
    lines.append("")
    lines.append(f"- Model: `{MODEL}`  temperature={TEMPERATURE}  runs/variant={RUNS_PER_VARIANT}")
    lines.append(f"- Fixture: cruz / cruz-wurtz duplicate pair + Lillian Diaz math workbook signal")
    lines.append(f"- Total cost: ${total_cost:.4f}")
    lines.append("")
    lines.append("## Headline table")
    lines.append("")
    lines.append("| Variant | Run | Dedup | Canonical | Links | FM | Reason | In tok | Out tok | Cost $ | Latency s |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for r in results:
        if not r.ok:
            lines.append(f"| {r.variant} | {r.run_idx} | ERROR | — | — | — | — | — | — | — | — |")
            continue
        lines.append(
            f"| {r.variant} | {r.run_idx} "
            f"| {'Y' if r.dedup_success else 'N'} "
            f"| {r.canonical_slug or '-'} "
            f"| {r.crosslink_count} "
            f"| {'Y' if r.frontmatter_present else 'N'} "
            f"| {'Y' if r.reason_sensible else 'N'} "
            f"| {r.input_tokens} | {r.output_tokens} "
            f"| {r.cost_usd:.5f} | {r.latency_ms/1000:.1f} |"
        )
    lines.append("")

    # Per-variant summary
    lines.append("## Per-variant summary")
    lines.append("")
    variants: dict[str, list[RunResult]] = {}
    for r in results:
        variants.setdefault(r.variant, []).append(r)

    summary_lookup: dict[str, dict] = {}
    for name, runs in variants.items():
        okr = [r for r in runs if r.ok]
        if not okr:
            lines.append(f"- **{name}**: all runs errored.")
            summary_lookup[name] = {"dedup_rate": 0.0, "avg_links": 0.0, "fm_rate": 0.0}
            continue
        dedup = sum(1 for r in okr if r.dedup_success)
        canonicals = [r.canonical_slug for r in okr if r.dedup_success]
        avg_links = sum(r.crosslink_count for r in okr) / len(okr)
        fm = sum(1 for r in okr if r.frontmatter_present)
        reason_ok = sum(1 for r in okr if r.reason_sensible)
        avg_cost = sum(r.cost_usd for r in okr) / len(okr)
        avg_lat = sum(r.latency_ms for r in okr) / len(okr) / 1000.0
        lines.append(
            f"- **{name}**: dedup {dedup}/{len(okr)}, canonical choices={canonicals or '—'}, "
            f"avg links={avg_links:.1f}, frontmatter {fm}/{len(okr)}, reason-sensible {reason_ok}/{len(okr)}, "
            f"avg cost=${avg_cost:.5f}, avg latency={avg_lat:.1f}s"
        )
        summary_lookup[name] = {
            "dedup_rate": dedup / len(okr),
            "avg_links": avg_links,
            "fm_rate": fm / len(okr),
        }
    lines.append("")

    # Verdict
    v1 = summary_lookup.get("V1_dedup", {"dedup_rate": 0.0})
    v2 = summary_lookup.get("V2_dedup_crosslink_fm", {"dedup_rate": 0.0, "avg_links": 0.0, "fm_rate": 0.0})

    if v1["dedup_rate"] >= 2 / 3 and v2.get("avg_links", 0) >= 1 and v2.get("fm_rate", 0) >= 2 / 3:
        verdict = "GREEN"
        reasoning = (
            f"V1 hit dedup in {v1['dedup_rate']*100:.0f}% of runs; "
            f"V2 averaged {v2['avg_links']:.1f} cross-links and "
            f"{v2['fm_rate']*100:.0f}% frontmatter compliance."
        )
    elif v1["dedup_rate"] <= 1 / 3:
        verdict = "RED"
        reasoning = (
            f"V1 only hit dedup in {v1['dedup_rate']*100:.0f}% of runs — Flash-Lite cannot "
            f"reliably reason about dedup inside integrate. Keep dedup in reflect or upgrade."
        )
    else:
        verdict = "YELLOW"
        reasoning = (
            f"Mixed results: V1 dedup {v1['dedup_rate']*100:.0f}%, "
            f"V2 avg links {v2.get('avg_links',0):.1f}, "
            f"V2 frontmatter {v2.get('fm_rate',0)*100:.0f}%. "
            f"Some jobs workable; shortlist carefully."
        )

    lines.append("## Verdict")
    lines.append("")
    lines.append(f"**{verdict}** — {reasoning}")
    lines.append("")

    # Fixture content
    lines.append("## Fixture signals_text")
    lines.append("")
    lines.append("```")
    lines.append(fixture["signals_text"].rstrip())
    lines.append("```")
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    asyncio.run(main())
