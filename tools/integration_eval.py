"""Integration cycle local model evaluation.

Replays captured integration fixtures through local models (via Ollama)
and compares against Gemini's responses.

Usage:
    ./venv/bin/python tools/integration_eval.py --model qwen3:8b
"""

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))

FIXTURES_DIR = Path.home() / ".deja" / "integration_fixtures"


def load_fixtures() -> list[dict]:
    """Load all input/response pairs."""
    fixtures = []
    for inp_path in sorted(FIXTURES_DIR.glob("*-messages.json")) + sorted(FIXTURES_DIR.glob("*-context.json")):
        resp_path = inp_path.parent / inp_path.name.replace(".json", "-response.json")
        if not resp_path.exists():
            continue
        fixtures.append({
            "name": inp_path.stem,
            "input": json.loads(inp_path.read_text()),
            "gemini_response": json.loads(resp_path.read_text()),
        })
    return fixtures


def build_prompt(fixture: dict, prompt_override: str | None = None) -> str:
    """Build the integration prompt from a fixture."""
    from deja.identity import load_user
    from deja.wiki_schema import load_schema
    from datetime import datetime

    now = datetime.now()
    user_fields = load_user().as_prompt_fields()
    schema = load_schema()

    if prompt_override:
        template = Path(prompt_override).read_text()
    else:
        from deja.prompts import load as load_prompt
        template = load_prompt("integrate")

    prompt = template.format(
        current_time=now.strftime("%Y-%m-%d %H:%M"),
        day_of_week=now.strftime("%A"),
        time_of_day="evening",
        contacts_text="(not available for eval)",
        schema=schema,
        goals="(not available for eval)",
        wiki_text=fixture["input"].get("wiki_text", ""),
        signals_text=fixture["input"].get("signals_text", ""),
        **user_fields,
    )
    return prompt


async def call_ollama(model: str, prompt: str, no_think: bool = False) -> tuple[str, int]:
    """Call Ollama with the integration prompt. Returns (response_text, latency_ms)."""
    import httpx

    # For thinking models, prepend /no_think
    actual_prompt = prompt
    if "gemma4" in model or no_think:
        actual_prompt = "/no_think\n" + prompt

    # Thinking models need more tokens for internal reasoning
    num_predict = 8192 if "gemma4" in model else 4096

    t0 = time.time()
    async with httpx.AsyncClient(timeout=300) as client:
        resp = await client.post(
            "http://localhost:11434/api/generate",
            json={
                "model": model,
                "prompt": actual_prompt,
                "stream": False,
                "options": {
                    "temperature": 0.2,
                    "num_predict": num_predict,
                },
                "format": "json",
            },
        )
        resp.raise_for_status()
        data = resp.json()

    latency_ms = int((time.time() - t0) * 1000)
    return data.get("response", ""), latency_ms


def evaluate_response(text: str, gemini_response: dict) -> dict:
    """Score a local model response against Gemini's."""
    # 1. Valid JSON?
    try:
        parsed = json.loads(text)
        valid_json = True
    except (json.JSONDecodeError, ValueError):
        valid_json = False
        parsed = {}

    # 2. Correct schema?
    has_reasoning = "reasoning" in parsed
    has_updates = "wiki_updates" in parsed
    correct_schema = has_reasoning and has_updates

    # 3. Update count comparison
    local_updates = len(parsed.get("wiki_updates", []))
    gemini_updates = len(gemini_response.get("wiki_updates", []))

    # 4. Reasoning present?
    reasoning = parsed.get("reasoning", "")

    return {
        "valid_json": valid_json,
        "correct_schema": correct_schema,
        "local_updates": local_updates,
        "gemini_updates": gemini_updates,
        "reasoning_length": len(reasoning),
        "response_length": len(text),
    }


async def main():
    parser = argparse.ArgumentParser(description="Integration cycle local model eval")
    parser.add_argument("--model", default="qwen3:8b", help="Ollama model name")
    parser.add_argument("--prompt", default=None, help="Path to custom prompt template file")
    parser.add_argument("--no-think", action="store_true", help="Prepend /no_think to prompt (for Qwen3)")
    args = parser.parse_args()

    fixtures = load_fixtures()
    if not fixtures:
        print(f"No fixtures found in {FIXTURES_DIR}")
        sys.exit(1)

    label = f"{args.model}"
    if args.prompt:
        label += f" + {Path(args.prompt).stem}"
    if args.no_think:
        label += " + /no_think"
    print(f"Evaluating {len(fixtures)} fixtures with {label}")
    print()

    results = []
    for fx in fixtures:
        prompt = build_prompt(fx, prompt_override=args.prompt)
        prompt_len = len(prompt)

        try:
            text, latency = await call_ollama(args.model, prompt, no_think=args.no_think)
            scores = evaluate_response(text, fx["gemini_response"])
            scores["latency_ms"] = latency
            scores["prompt_chars"] = prompt_len
            results.append(scores)

            status = "✓" if scores["valid_json"] and scores["correct_schema"] else "✗"
            print(
                f"  {status} {fx['name']:45s} "
                f"{latency/1000:.1f}s  "
                f"json={'Y' if scores['valid_json'] else 'N'}  "
                f"schema={'Y' if scores['correct_schema'] else 'N'}  "
                f"updates={scores['local_updates']}(gemini={scores['gemini_updates']})  "
                f"prompt={prompt_len}"
            )
        except Exception as e:
            print(f"  ✗ {fx['name']:45s} ERROR: {e}")
            results.append({"valid_json": False, "correct_schema": False, "latency_ms": 0, "local_updates": 0, "gemini_updates": 0})

    # Aggregate
    print()
    print("=" * 70)
    total = len(results)
    valid = sum(1 for r in results if r.get("valid_json"))
    schema_ok = sum(1 for r in results if r.get("correct_schema"))
    avg_latency = sum(r.get("latency_ms", 0) for r in results) / max(total, 1)
    total_local = sum(r.get("local_updates", 0) for r in results)
    total_gemini = sum(r.get("gemini_updates", 0) for r in results)

    print(f"Model: {args.model}")
    print(f"Valid JSON:     {valid}/{total} ({valid/total*100:.0f}%)")
    print(f"Correct schema: {schema_ok}/{total} ({schema_ok/total*100:.0f}%)")
    print(f"Avg latency:    {avg_latency/1000:.1f}s")
    print(f"Wiki updates:   {total_local} local vs {total_gemini} Gemini")


if __name__ == "__main__":
    asyncio.run(main())
