"""Test 2 — V2 principles (cross-link + frontmatter) on production integrate.

Runs control (~/Deja/prompts/integrate.md) vs
integrate_v2_shippable.md three times each on gemini-2.5-flash-lite
against a real integrate fixture, then compares updates, cross-link
density, frontmatter rate, cost, latency.

Usage: ./venv/bin/python tools/integrate_v2_ship_test.py
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))

# Real fixture with substantive signals (outbound email thread about Apex
# + iMessage about Cantera shop deck measurements + more). Selected as
# the largest recent messages fixture in ~/.deja/integration_fixtures/.
FIXTURE_MESSAGES = Path.home() / ".deja" / "integration_fixtures" / "20260410-175009-messages.json"

PROMPT_CONTROL = Path.home() / "Deja" / "prompts" / "integrate.md"
PROMPT_V2 = _REPO / "src" / "deja" / "prompts" / "integrate_v2_shippable.md"
OUT_DIR = Path.home() / ".deja" / "integrate_v2_ship_test"

MODEL = "gemini-2.5-flash-lite"
RUNS_PER_VARIANT = 3

# Flash-Lite pricing
PRICE_IN = 0.10 / 1_000_000
PRICE_OUT = 0.40 / 1_000_000


def build_fixture() -> dict:
    from deja.identity import load_user
    from deja.wiki_schema import load_schema
    from deja.observations.contacts import get_contacts_summary

    raw = json.loads(FIXTURE_MESSAGES.read_text())
    wiki_text = raw.get("wiki_text", "")
    signals_text = raw.get("signals_text", "")

    user_fields = load_user().as_prompt_fields()
    schema = load_schema()
    try:
        contacts_text = get_contacts_summary()
    except Exception:
        contacts_text = ""

    now = datetime.now()
    return {
        "current_time": now.strftime("%Y-%m-%d %H:%M"),
        "day_of_week": now.strftime("%A"),
        "time_of_day": "morning",
        "contacts_text": contacts_text,
        "schema": schema,
        "wiki_text": wiki_text,
        "signals_text": signals_text,
        **user_fields,
    }


async def run_once(prompt_text: str, variant: str, run_idx: int) -> dict:
    from deja.llm_client import GeminiClient, _parse_json

    client = GeminiClient()
    t0 = time.time()
    try:
        resp = await client._generate_full(
            model=MODEL,
            contents=prompt_text,
            config_dict={
                "response_mime_type": "application/json",
                "max_output_tokens": 16384,
                "temperature": 0.2,
            },
        )
    except Exception:
        await asyncio.sleep(10)
        resp = await client._generate_full(
            model=MODEL,
            contents=prompt_text,
            config_dict={
                "response_mime_type": "application/json",
                "max_output_tokens": 16384,
                "temperature": 0.2,
            },
        )
    latency_ms = int((time.time() - t0) * 1000)

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

    if not isinstance(parsed, dict):
        parsed = {}

    updates = parsed.get("wiki_updates") or []
    if not isinstance(updates, list):
        updates = []

    writes = [u for u in updates if isinstance(u, dict) and (u.get("action") or "").lower() in ("update", "create")]
    total_content = "\n".join((u.get("content") or "") for u in writes)
    crosslink_count = len(re.findall(r"\[\[[^\]]+\]\]", total_content))
    fm_count = sum(1 for u in writes if (u.get("content") or "").lstrip().startswith("---"))
    fm_rate = (fm_count / len(writes)) if writes else 0.0
    crosslink_density = (crosslink_count / len(writes)) if writes else 0.0

    return {
        "variant": variant,
        "run_idx": run_idx,
        "ok": bool(parsed),
        "latency_ms": latency_ms,
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "cost_usd": in_tok * PRICE_IN + out_tok * PRICE_OUT,
        "num_updates": len(updates),
        "num_writes": len(writes),
        "crosslink_count": crosslink_count,
        "crosslink_density": crosslink_density,
        "frontmatter_count": fm_count,
        "frontmatter_rate": fm_rate,
        "reasoning": (parsed.get("reasoning") or "")[:500],
        "first_update_preview": (writes[0].get("content") or "")[:400] if writes else "",
        "first_update_slug": writes[0].get("slug") if writes else "",
        "raw": raw,
    }


async def main() -> None:
    print(f"Integrate V2_shippable test — model={MODEL}, {RUNS_PER_VARIANT} runs/variant")
    print(f"Fixture: {FIXTURE_MESSAGES}")

    fixture = build_fixture()
    print(f"  wiki_text: {len(fixture['wiki_text'])} chars")
    print(f"  signals_text: {len(fixture['signals_text'])} chars")

    ctrl_template = PROMPT_CONTROL.read_text()
    v2_template = PROMPT_V2.read_text()

    ctrl_prompt = ctrl_template.format(**fixture)
    v2_prompt = v2_template.format(**fixture)
    print(f"  control prompt chars: {len(ctrl_prompt)}")
    print(f"  v2 prompt chars: {len(v2_prompt)}")

    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    out_dir = OUT_DIR / ts
    out_dir.mkdir(parents=True, exist_ok=True)

    # Run sequentially — parallel 40K+ token calls against the proxy
    # race past the httpx 120s read timeout when queued together.
    results = []
    specs = [(ctrl_prompt, "control", i) for i in range(1, RUNS_PER_VARIANT + 1)]
    specs += [(v2_prompt, "v2_shippable", i) for i in range(1, RUNS_PER_VARIANT + 1)]
    for prompt, variant, i in specs:
        print(f"  {variant} run {i}...", flush=True)
        r = await run_once(prompt, variant, i)
        print(f"    done: ok={r['ok']}, updates={r.get('num_updates', 0)}, "
              f"{r['latency_ms']/1000:.1f}s, ${r.get('cost_usd', 0):.5f}", flush=True)
        results.append(r)

    (out_dir / "results.json").write_text(json.dumps(results, indent=2, ensure_ascii=False))

    def summarize(variant: str) -> dict:
        rs = [r for r in results if r["variant"] == variant]
        ok = [r for r in rs if r["ok"]]
        return {
            "n": len(rs),
            "ok": len(ok),
            "avg_updates": sum(r["num_updates"] for r in ok) / max(1, len(ok)),
            "avg_crosslink_density": sum(r["crosslink_density"] for r in ok) / max(1, len(ok)),
            "avg_frontmatter_rate": sum(r["frontmatter_rate"] for r in ok) / max(1, len(ok)),
            "avg_cost": sum(r["cost_usd"] for r in ok) / max(1, len(ok)),
            "avg_latency_s": sum(r["latency_ms"] for r in ok) / max(1, len(ok)) / 1000.0,
            "total_cost": sum(r["cost_usd"] for r in ok),
        }

    ctrl = summarize("control")
    v2 = summarize("v2_shippable")

    print()
    print("=" * 70)
    print(f"{'metric':<22} {'control':>14} {'v2_shippable':>16}")
    print("-" * 70)
    print(f"{'parse_ok':<22} {ctrl['ok']}/{ctrl['n']:<12} {v2['ok']}/{v2['n']:<14}")
    print(f"{'avg updates':<22} {ctrl['avg_updates']:>14.2f} {v2['avg_updates']:>16.2f}")
    print(f"{'avg crosslink dens':<22} {ctrl['avg_crosslink_density']:>14.2f} {v2['avg_crosslink_density']:>16.2f}")
    print(f"{'avg frontmatter rate':<22} {ctrl['avg_frontmatter_rate']:>14.2f} {v2['avg_frontmatter_rate']:>16.2f}")
    print(f"{'avg cost ($)':<22} {ctrl['avg_cost']:>14.5f} {v2['avg_cost']:>16.5f}")
    print(f"{'avg latency (s)':<22} {ctrl['avg_latency_s']:>14.2f} {v2['avg_latency_s']:>16.2f}")
    print()
    print(f"Total cost: control ${ctrl['total_cost']:.4f} | v2 ${v2['total_cost']:.4f}")
    print()
    print("--- per-run detail ---")
    for r in results:
        print(f"  [{r['variant']} run{r['run_idx']}] updates={r['num_updates']} "
              f"links={r['crosslink_count']} fm={r['frontmatter_count']}/{r['num_writes']} "
              f"tok={r['input_tokens']}/{r['output_tokens']} ${r['cost_usd']:.5f} {r['latency_ms']/1000:.1f}s")
        print(f"    first_slug={r['first_update_slug']}")
        print(f"    reasoning: {r['reasoning'][:160]}")
    print()
    print(f"Output dir: {out_dir}")


if __name__ == "__main__":
    asyncio.run(main())
