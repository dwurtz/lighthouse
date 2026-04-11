"""Reflection pass A/B evaluation harness.

Runs the same reflection inputs through multiple Gemini models and produces
a side-by-side markdown report so a human can judge quality. Three phases:

1. ``capture`` — snapshot the current reflection inputs (the exact 7 fields
   the live reflection pass assembles) into a JSON fixture under
   ``~/.deja/reflection_fixtures/<timestamp>.json``. Walks the same helper
   functions ``_run_reflection_body()`` uses.

2. ``compare`` — load a fixture and run it through each specified model,
   saving per-model outputs under ``~/.deja/reflection_eval/<timestamp>/``.
   Supports ``--runs N`` for self-consistency checks and ``--dry-run`` for
   smoke-testing the full pipeline without calling cloud APIs.

3. ``report`` — read the per-model JSON files from a previous ``compare``
   run and produce a markdown report: headline table, self-consistency
   analysis, cross-model divergence, thoughts side-by-side, and wiki
   update details.

This harness never touches production code. It is intentionally parallel
to ``tools/vision_eval.py`` but the metric domain is different (text /
JSON schema conformance rather than visual grounding).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

# Ensure ``deja`` is importable when run from the repo root.
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))

from deja.config import DEJA_HOME  # noqa: E402


# ---------------------------------------------------------------------------
# Model rates — hardcoded per user's spec (USD per 1M tokens)
# ---------------------------------------------------------------------------

MODEL_RATES: dict[str, dict[str, float]] = {
    "gemini-2.5-flash": {
        "input_per_mtok_usd": 0.30,
        "output_per_mtok_usd": 2.50,
    },
    "gemini-2.5-pro": {
        "input_per_mtok_usd": 1.25,
        "output_per_mtok_usd": 10.00,
    },
    # 3.1 Pro pricing is unannounced; assume parity with 2.5 Pro for now.
    "gemini-3.1-pro-preview": {
        "input_per_mtok_usd": 1.25,
        "output_per_mtok_usd": 10.00,
    },
}

DEFAULT_MODELS = "gemini-2.5-flash,gemini-2.5-pro,gemini-3.1-pro-preview"

FIXTURE_DIR = DEJA_HOME / "reflection_fixtures"
EVAL_DIR = DEJA_HOME / "reflection_eval"

FIXTURE_VERSION = 1
PROMPT_NAME = "reflect"

# QMD retrieval parameters for --wiki-mode retrieval.
# User direction: cap query at ~2000 chars (the production retriever uses
# 400 because it also runs through HyDE expansion every few minutes and
# latency matters; for eval we can afford 2000).
QMD_QUERY_CHAR_CAP = 2000
QMD_TOP_N = 15
QMD_COLLECTION = "Deja"


# ---------------------------------------------------------------------------
# Prompt field assembly — the 7 inputs _run_reflection_body() passes plus
# the derived fields (current_time, user_fields). We snapshot everything we
# can so a replay is deterministic.
# ---------------------------------------------------------------------------


def _slugify_model(model: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", model.lower()).strip("-")


def _gather_reflection_fields() -> dict:
    """Assemble the same fields ``_run_reflection_body()`` feeds to the
    reflect prompt. Fails loudly if any helper is unavailable.
    """
    try:
        from deja import wiki as wiki_store
        from deja.config import WIKI_DIR
        from deja.reflection import (
            _recent_signals_text,
            _recent_events_text,
            _find_orphan_people_with_contacts,
            _format_orphan_candidates,
        )
        from deja.observations.contacts import get_contacts_summary
        from deja.wiki_schema import load_schema
        from deja.identity import load_user
    except ImportError as e:
        raise RuntimeError(
            f"Failed to import a reflection helper — the harness is out of "
            f"sync with src/deja/reflection.py. Error: {e}"
        ) from e

    wiki_store.ensure_dirs()

    wiki_text = wiki_store.render_for_prompt()
    signals_text = _recent_signals_text()

    goals_path = WIKI_DIR / "goals.md"
    goals_text = goals_path.read_text() if goals_path.exists() else "(no goals.md)"

    events_text = _recent_events_text(days=7)

    contacts_text = get_contacts_summary()

    orphan_candidates = _find_orphan_people_with_contacts()
    orphan_text = _format_orphan_candidates(orphan_candidates)

    schema = load_schema()
    user_fields = load_user().as_prompt_fields()

    # current_time is a live-clock field; capture it so replays are reproducible.
    current_time = datetime.now().strftime("%A, %B %d, %Y — %H:%M")

    fields: dict = {
        "current_time": current_time,
        "contacts_text": contacts_text,
        "schema": schema,
        "goals": goals_text,
        "wiki_text": wiki_text,
        "recent_events": events_text,
        "recent_observations": signals_text,
        "orphan_people": orphan_text,
    }
    # Merge user_fields last — same order as reflection.py.
    fields.update(user_fields)
    return fields


def _format_reflect_prompt(template: str, fields: dict) -> str:
    """Render the reflect template. If the template references any field
    that the fixture does not provide, raise with a clear message listing
    the missing fields.
    """
    # Find all {name} placeholders, ignoring escaped {{ and }} (used as
    # literal JSON braces in the output-schema block of reflect.md).
    placeholder_re = re.compile(r"(?<!\{)\{([a-zA-Z_][a-zA-Z0-9_]*)\}(?!\})")
    required = set(placeholder_re.findall(template))
    missing = sorted(required - set(fields.keys()))
    if missing:
        raise KeyError(
            f"Reflect prompt template expects fields that are not in the "
            f"fixture: {missing}. Available fields: {sorted(fields.keys())}"
        )
    return template.format(**fields)


# ---------------------------------------------------------------------------
# Response parsing — tolerant JSON extractor, mirrors llm_client._parse_json
# ---------------------------------------------------------------------------


def _parse_reflect_response(raw: str) -> dict | None:
    """Parse a reflection model response. Returns None on failure."""
    if not raw:
        return None
    text = raw.strip()
    # Strip markdown fences (```json ... ``` or plain ```).
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    # Fall back: find outermost object braces.
    if "{" in text and "}" in text:
        start = text.index("{")
        end = text.rindex("}") + 1
        try:
            obj = json.loads(text[start:end])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            return None
    return None


# ---------------------------------------------------------------------------
# Cost estimation
# ---------------------------------------------------------------------------


def _estimate_cost_usd(model: str, input_tok: int, output_tok: int) -> float:
    rates = MODEL_RATES.get(model)
    if not rates:
        return 0.0
    in_cost = (input_tok / 1_000_000) * rates["input_per_mtok_usd"]
    out_cost = (output_tok / 1_000_000) * rates["output_per_mtok_usd"]
    return round(in_cost + out_cost, 6)


# ---------------------------------------------------------------------------
# Retrieval-mode wiki assembly (condition E / F)
# ---------------------------------------------------------------------------


def _extract_qmd_query_text(fields: dict) -> str:
    """Assemble a QMD query string from the fixture's recent signal text.

    We use ``recent_observations`` (the same per-cycle signal dump that
    the real reflection pass sees) as the retrieval query — it's the most
    recent user activity and is exactly what the prompt is reasoning
    about. Cap the total query at ``QMD_QUERY_CHAR_CAP``.

    ``qmd query`` parses multi-line input as a typed query document
    (lines prefixed ``lex:``/``vec:``/``hyde:``) — a raw signal dump
    trips the parser on the very first line. We flatten newlines to
    spaces and collapse whitespace so the whole thing is one implicit
    ``expand`` query.
    """
    text = (fields.get("recent_observations") or "").strip()
    if not text:
        # Fall back to recent events if observations are empty.
        text = (fields.get("recent_events") or "").strip()
    # Flatten: no newlines, no pipe/prefix chars that look like a typed-line.
    text = text.replace("\r", " ").replace("\n", " ")
    text = re.sub(r"\s+", " ", text).strip()
    # Strip bracket-prefixed timestamp tokens that dominate the feed —
    # they're pure noise to retrieval.
    text = re.sub(r"\[\d{4}-\d{2}-\d{2}T[\d:]+\]\s*", "", text)
    text = re.sub(r"\[[a-z_]+\]\s*", "", text)
    if len(text) > QMD_QUERY_CHAR_CAP:
        text = text[:QMD_QUERY_CHAR_CAP]
    return text


def _parse_qmd_files_output(raw: str) -> list[str]:
    """Parse ``qmd query ... --files`` stdout into a list of wiki paths.

    Output format (one per line):
        ``#hexcolor,score,qmd://Deja/<relpath>.md``
    We skip the progress / 'Expanding query' banner lines and any line
    that doesn't start with a ``#`` color token.
    """
    paths: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or not line.startswith("#"):
            continue
        parts = line.split(",", 2)
        if len(parts) != 3:
            continue
        uri = parts[2].strip()
        if not uri.startswith("qmd://"):
            continue
        # qmd://Deja/people/foo.md -> people/foo.md
        rest = uri[len("qmd://"):]
        if "/" in rest:
            rest = rest.split("/", 1)[1]
        paths.append(rest)
    return paths


def _run_qmd_query(query_text: str, *, top_n: int) -> list[str]:
    """Invoke ``qmd query ... --files`` and return relative wiki paths."""
    import subprocess
    if not query_text.strip():
        return []
    cmd = [
        "/opt/homebrew/bin/qmd", "query", query_text,
        "-n", str(top_n),
        "-c", QMD_COLLECTION,
        "--files",
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except Exception as e:
        print(f"  qmd error: {e}", file=sys.stderr)
        return []
    if r.returncode != 0:
        print(f"  qmd nonzero exit ({r.returncode}): {r.stderr[:200]}", file=sys.stderr)
        return []
    return _parse_qmd_files_output(r.stdout)


def _build_retrieval_wiki_text(
    fields: dict,
    *,
    index_max_lines: int | None,
) -> tuple[str, dict]:
    """Build the retrieval-mode ``wiki_text`` payload.

    Returns ``(wiki_text, info)`` where ``info`` is a dict of retrieval
    metadata (query length, number of pages found, bytes read) so the
    per-condition output JSON can record how retrieval behaved.
    """
    from deja import wiki as wiki_store  # noqa: F401 — ensures wiki dirs
    from deja.config import WIKI_DIR
    from deja.wiki_catalog import render_index_for_prompt

    wiki_store.ensure_dirs()

    # 1) Index (catalog of every page)
    index_text = render_index_for_prompt(max_lines=index_max_lines, rebuild=False)
    if not index_text:
        index_text = render_index_for_prompt(max_lines=index_max_lines, rebuild=True)

    # 2) Run QMD
    query_text = _extract_qmd_query_text(fields)
    rel_paths = _run_qmd_query(query_text, top_n=QMD_TOP_N)

    # 3) Read page bodies (dedup, skip meta files)
    page_bodies: list[str] = []
    seen: set[str] = set()
    total_bytes = 0
    skipped_meta = 0
    skipped_missing = 0
    for rp in rel_paths:
        base = rp.rsplit("/", 1)[-1].lower()
        if base in {"index.md", "log.md", "claude.md", "reflection.md"}:
            skipped_meta += 1
            continue
        if rp in seen:
            continue
        seen.add(rp)
        page_path = WIKI_DIR / rp
        try:
            body = page_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            skipped_missing += 1
            continue
        slug = Path(rp).stem
        page_bodies.append(f"# {slug}\n\n{body.rstrip()}\n")
        total_bytes += len(body)

    # 4) Assemble
    parts = [index_text.rstrip(), "", "## Relevant pages (retrieved)", ""]
    parts.extend(page_bodies)
    wiki_text = "\n".join(parts)

    info = {
        "wiki_mode": "retrieval",
        "index_max_lines": index_max_lines,
        "index_chars": len(index_text),
        "qmd_query_chars": len(query_text),
        "qmd_paths_returned": len(rel_paths),
        "pages_included": len(page_bodies),
        "pages_skipped_meta": skipped_meta,
        "pages_skipped_missing": skipped_missing,
        "page_bytes_total": total_bytes,
        "wiki_text_chars": len(wiki_text),
    }
    return wiki_text, info


# ---------------------------------------------------------------------------
# capture subcommand
# ---------------------------------------------------------------------------


def cmd_capture(args) -> None:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)

    fields = _gather_reflection_fields()

    captured_at = datetime.now().isoformat()
    fixture = {
        "fixture_version": FIXTURE_VERSION,
        "prompt_name": PROMPT_NAME,
        "captured_at": captured_at,
        "fields": fields,
    }

    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    out_path = FIXTURE_DIR / f"{ts}.json"
    out_path.write_text(json.dumps(fixture, indent=2, ensure_ascii=False))
    size = out_path.stat().st_size
    print(f"captured fixture: {out_path}  ({size} bytes)")
    print(f"field keys: {', '.join(sorted(fields.keys()))}")


# ---------------------------------------------------------------------------
# compare subcommand
# ---------------------------------------------------------------------------


def _latest_fixture() -> Path | None:
    if not FIXTURE_DIR.exists():
        return None
    candidates = sorted(FIXTURE_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime)
    return candidates[-1] if candidates else None


def _latest_eval_dir() -> Path | None:
    if not EVAL_DIR.exists():
        return None
    candidates = [p for p in EVAL_DIR.iterdir() if p.is_dir()]
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime)
    return candidates[-1]


def _dry_run_response(model: str, run_idx: int, fields: dict) -> str:
    """Generate a synthetic response that mimics the reflect output schema.

    Each model/run produces slightly different pages so the report has
    something interesting to show: self-consistency and cross-model
    divergence tables won't be completely empty.
    """
    # Pick a handful of candidate slugs from the fixture's wiki_text so
    # the synthetic output touches plausible pages.
    wiki_text = fields.get("wiki_text") or ""
    headings = re.findall(r"^#+\s*([a-zA-Z0-9_\-/]+)", wiki_text, flags=re.MULTILINE)
    fallback_slugs = [
        "projects/alpha",
        "projects/beta",
        "people/example-person",
        "projects/gamma",
    ]
    slugs = headings[:6] or fallback_slugs

    # Deterministic per-(model, run) selection so the output is stable.
    seed = abs(hash(f"{model}:{run_idx}")) % (2**32)
    rng_state = seed
    def _pick(pool, n):
        nonlocal rng_state
        picked = []
        pool = list(pool)
        for _ in range(min(n, len(pool))):
            rng_state = (rng_state * 1103515245 + 12345) & 0x7FFFFFFF
            idx = rng_state % len(pool)
            picked.append(pool.pop(idx))
        return picked

    chosen = _pick(slugs, 3)

    updates = []
    for slug in chosen:
        if "/" in slug:
            category, s = slug.split("/", 1)
        else:
            category, s = "projects", slug
        # Normalize category.
        if category not in ("people", "projects"):
            category = "projects"
        updates.append({
            "category": category,
            "slug": s,
            "action": "update",
            "content": f"# {s}\n\nSynthetic content from {model} run {run_idx}.",
            "reason": f"dry-run synthetic update from {model}/{run_idx}",
        })

    payload = {
        "wiki_updates": updates,
        "goal_actions": [
            {
                "kind": "calendar_create",
                "params": {
                    "summary": f"Synthetic goal from {model}",
                    "start": "2026-04-11T09:00:00-07:00",
                    "end": "2026-04-11T10:00:00-07:00",
                },
                "reason": "dry-run placeholder",
            }
        ] if run_idx == 0 else [],
        "thoughts": (
            f"## What stands out\n\nDry-run synthetic thoughts from "
            f"`{model}` run #{run_idx}. This is not a real reflection; "
            f"it exists only so the harness can smoke-test the full "
            f"pipeline without spending money on cloud API calls.\n\n"
            f"## A question for you\n\nAre the report sections rendering "
            f"correctly?\n"
        ),
    }
    return json.dumps(payload, indent=2)


def _extract_usage(resp: dict | object) -> tuple[int, int, bool]:
    """Return (input_tokens, output_tokens, real) from a _generate_full response.

    Real proxy responses are dicts with a ``usage_metadata`` sub-dict that
    has ``prompt_token_count``, ``candidates_token_count``, and (for
    thinking models) ``thoughts_token_count``. Direct-mode responses are
    google-genai objects with the same attributes. We count thoughts as
    output tokens — they're billed at the output rate for Gemini 2.5 Pro
    / 3.1 Pro thinking mode.
    """
    um = None
    if isinstance(resp, dict):
        um = resp.get("usage_metadata")
    else:
        um = getattr(resp, "usage_metadata", None)
    if not um:
        return 0, 0, False

    def _get(key):
        if isinstance(um, dict):
            return um.get(key) or 0
        return getattr(um, key, 0) or 0

    in_tok = int(_get("prompt_token_count") or 0)
    out_tok = int(_get("candidates_token_count") or 0)
    thoughts = int(_get("thoughts_token_count") or 0)
    out_tok += thoughts
    return in_tok, out_tok, True


def _extract_text(resp: dict | object) -> str:
    """Pull the text body out of a _generate_full response."""
    if isinstance(resp, dict):
        return resp.get("text") or ""
    return getattr(resp, "text", "") or ""


async def _call_model(
    model: str,
    prompt: str,
    *,
    dry_run: bool,
    run_idx: int,
    fields: dict,
    wiki_mode: str,
    retrieval_info: dict | None,
) -> dict:
    """Call one model once. Returns a dict ready to be serialized."""
    result: dict = {
        "model": model,
        "run_index": run_idx,
        "wiki_mode": wiki_mode,
        "retrieval_info": retrieval_info,
        "prompt_chars": len(prompt),
        "raw_response": "",
        "parsed": None,
        "latency_ms": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "token_source": "none",
        "estimated_cost_usd": 0.0,
        "error": None,
    }

    t0 = time.time()
    try:
        if dry_run:
            raw = _dry_run_response(model, run_idx, fields)
            # Fake some plausible token counts so the cost column has data.
            # ~4 chars per token is the standard Gemini heuristic.
            in_tok = max(1, len(prompt) // 4)
            out_tok = max(1, len(raw) // 4)
            token_source = "dry_run_estimate"
        else:
            from deja.llm_client import GeminiClient
            client = GeminiClient()
            resp = await client._generate_full(
                model=model,
                contents=prompt,
                config_dict={
                    "response_mime_type": "application/json",
                    "max_output_tokens": 65536,
                    "temperature": 0.3,
                },
            )
            raw = _extract_text(resp)
            in_tok, out_tok, real_tokens = _extract_usage(resp)
            if real_tokens:
                token_source = "usage_metadata"
            else:
                # Fallback: char/4 heuristic, same as vision_eval.py.
                in_tok = max(1, len(prompt) // 4)
                out_tok = max(1, len(raw) // 4)
                token_source = "char_estimate"

        latency = int((time.time() - t0) * 1000)
        result["latency_ms"] = latency
        result["raw_response"] = raw
        result["input_tokens"] = in_tok
        result["output_tokens"] = out_tok
        result["token_source"] = token_source
        result["estimated_cost_usd"] = _estimate_cost_usd(model, in_tok, out_tok)
        result["parsed"] = _parse_reflect_response(raw)
    except Exception as e:  # noqa: BLE001 — per-model isolation is the point.
        result["latency_ms"] = int((time.time() - t0) * 1000)
        result["error"] = f"{type(e).__name__}: {e}"

    return result


async def _run_compare(args) -> Path:
    fixture_path = Path(args.fixture) if args.fixture else _latest_fixture()
    if not fixture_path or not fixture_path.exists():
        raise SystemExit(
            f"no fixture found (looked in {FIXTURE_DIR}). "
            f"Run `reflection_eval.py capture` first."
        )

    fixture = json.loads(fixture_path.read_text())
    if fixture.get("fixture_version") != FIXTURE_VERSION:
        print(
            f"warning: fixture version {fixture.get('fixture_version')} != "
            f"harness version {FIXTURE_VERSION}",
            file=sys.stderr,
        )
    fields = dict(fixture["fields"])  # shallow copy — we may overwrite wiki_text

    wiki_mode = getattr(args, "wiki_mode", "full")
    index_max_lines = getattr(args, "index_max_lines", None)
    retrieval_info: dict | None = None

    if wiki_mode == "retrieval":
        retrieval_wiki_text, retrieval_info = _build_retrieval_wiki_text(
            fields, index_max_lines=index_max_lines,
        )
        fields["wiki_text"] = retrieval_wiki_text
        print(
            f"retrieval wiki assembly: "
            f"index={retrieval_info['index_chars']}ch, "
            f"pages={retrieval_info['pages_included']}/{retrieval_info['qmd_paths_returned']}, "
            f"total={retrieval_info['wiki_text_chars']}ch"
        )

    from deja.prompts import load as load_prompt
    template = load_prompt(PROMPT_NAME)
    prompt = _format_reflect_prompt(template, fields)

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    runs = max(1, int(args.runs))
    run_suffix = getattr(args, "run_suffix", "") or ""

    # Allow writing into an existing run directory so multiple compare
    # invocations can share a single output folder (used to run the 6
    # experiment conditions into one dir).
    if getattr(args, "run_dir", None):
        out_dir = Path(args.run_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        run_ts = out_dir.name
    else:
        run_ts = datetime.now().strftime("%Y%m%dT%H%M%S")
        out_dir = EVAL_DIR / run_ts
        out_dir.mkdir(parents=True, exist_ok=True)

    # Save metadata about this compare run so report can reconstruct it.
    # If meta.json already exists (shared run dir), merge rather than clobber.
    meta_path = out_dir / "meta.json"
    meta_entry = {
        "invocation_at": datetime.now().isoformat(),
        "fixture_path": str(fixture_path),
        "models": models,
        "runs": runs,
        "dry_run": bool(args.dry_run),
        "wiki_mode": wiki_mode,
        "index_max_lines": index_max_lines,
        "run_suffix": run_suffix,
        "prompt_chars": len(prompt),
        "retrieval_info": retrieval_info,
    }
    if meta_path.exists():
        try:
            existing = json.loads(meta_path.read_text())
        except json.JSONDecodeError:
            existing = {}
        invocations = existing.get("invocations") or []
        invocations.append(meta_entry)
        existing["invocations"] = invocations
        existing.setdefault("run_timestamp", run_ts)
        meta_path.write_text(json.dumps(existing, indent=2))
    else:
        meta_path.write_text(json.dumps({
            "run_timestamp": run_ts,
            "started_at": datetime.now().isoformat(),
            "invocations": [meta_entry],
        }, indent=2))

    print(f"compare run: {run_ts}")
    print(f"  fixture: {fixture_path}")
    print(f"  models: {models}")
    print(f"  runs per model: {runs}")
    print(f"  wiki_mode: {wiki_mode}  index_max_lines: {index_max_lines}")
    print(f"  run_suffix: {run_suffix!r}")
    print(f"  dry_run: {bool(args.dry_run)}")
    print(f"  prompt chars: {len(prompt)}")
    print()

    for model in models:
        for run_idx in range(runs):
            result = await _call_model(
                model, prompt,
                dry_run=bool(args.dry_run),
                run_idx=run_idx,
                fields=fields,
                wiki_mode=wiki_mode,
                retrieval_info=retrieval_info,
            )
            slug = _slugify_model(model)
            fname = f"{slug}_run{run_idx}{run_suffix}.json"
            (out_dir / fname).write_text(
                json.dumps(result, indent=2, ensure_ascii=False)
            )

            if result["error"]:
                print(
                    f"  {model:32s} run{run_idx}{run_suffix}  "
                    f"ERROR {result['latency_ms']}ms  "
                    f"{result['error'][:120]}"
                )
            else:
                parsed = result["parsed"] or {}
                n_updates = len(parsed.get("wiki_updates") or [])
                thoughts_len = len(parsed.get("thoughts") or "")
                print(
                    f"  {model:32s} run{run_idx}{run_suffix}  "
                    f"{result['latency_ms']:>6d}ms  "
                    f"in={result['input_tokens']:>7d}  "
                    f"out={result['output_tokens']:>6d}  "
                    f"${result['estimated_cost_usd']:.4f}  "
                    f"updates={n_updates}  thoughts={thoughts_len}ch  "
                    f"({result['token_source']})"
                )

    print()
    print(f"per-model outputs saved to {out_dir}")
    return out_dir


def cmd_compare(args) -> None:
    asyncio.run(_run_compare(args))


# ---------------------------------------------------------------------------
# report subcommand
# ---------------------------------------------------------------------------


@dataclass
class ModelRun:
    model: str
    run_index: int
    path: Path
    data: dict

    @property
    def parsed(self) -> dict:
        return self.data.get("parsed") or {}

    @property
    def ok(self) -> bool:
        return self.data.get("error") is None and self.parsed is not None and bool(self.parsed)

    @property
    def wiki_pages(self) -> set[str]:
        pages = set()
        for u in (self.parsed.get("wiki_updates") or []):
            cat = u.get("category", "")
            slug = u.get("slug", "")
            if cat and slug:
                pages.add(f"{cat}/{slug}")
        return pages


def _load_run_dir(run_dir: Path) -> list[ModelRun]:
    runs: list[ModelRun] = []
    for p in sorted(run_dir.glob("*_run*.json")):
        if p.name == "meta.json":
            continue
        try:
            data = json.loads(p.read_text())
        except json.JSONDecodeError:
            continue
        runs.append(ModelRun(
            model=data.get("model", p.stem),
            run_index=int(data.get("run_index", 0)),
            path=p,
            data=data,
        ))
    return runs


def _overlap_stats(a: set[str], b: set[str]) -> dict:
    only_a = sorted(a - b)
    only_b = sorted(b - a)
    both = sorted(a & b)
    union = a | b
    jaccard = (len(a & b) / len(union)) if union else 1.0
    return {
        "only_a": only_a,
        "only_b": only_b,
        "both": both,
        "jaccard": jaccard,
        "union_size": len(union),
    }


def _truncate(s: str, n: int) -> str:
    s = (s or "").replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _build_report(run_dir: Path) -> str:
    runs = _load_run_dir(run_dir)
    meta_path = run_dir / "meta.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}

    lines: list[str] = []
    lines.append(f"# Reflection eval report — {run_dir.name}")
    lines.append("")
    lines.append(f"- Run directory: `{run_dir}`")
    if meta:
        lines.append(f"- Fixture: `{meta.get('fixture_path', '?')}`")
        lines.append(f"- Models: {', '.join(meta.get('models', []))}")
        lines.append(f"- Runs per model: {meta.get('runs', '?')}")
        lines.append(f"- Dry run: {meta.get('dry_run', False)}")
        lines.append(f"- Prompt chars: {meta.get('prompt_chars', '?')}")
    lines.append("")

    # ---- 1. Headline table
    lines.append("## 1. Headline")
    lines.append("")
    lines.append("| Model | Run | Parse OK | Latency (s) | Input tok | Output tok | Cost | # wiki_updates | # goal_actions | thoughts len |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for r in runs:
        parsed = r.parsed
        parse_ok = "yes" if r.ok else "no"
        latency_s = r.data.get("latency_ms", 0) / 1000.0
        n_updates = len(parsed.get("wiki_updates") or [])
        n_actions = len(parsed.get("goal_actions") or [])
        thoughts_len = len(parsed.get("thoughts") or "")
        lines.append(
            f"| {r.model} | {r.run_index} | {parse_ok} | "
            f"{latency_s:.2f} | {r.data.get('input_tokens', 0)} | "
            f"{r.data.get('output_tokens', 0)} | "
            f"${r.data.get('estimated_cost_usd', 0.0):.4f} | "
            f"{n_updates} | {n_actions} | {thoughts_len} |"
        )
    lines.append("")

    # ---- 2. Self-consistency
    lines.append("## 2. Self-consistency (same model, multiple runs)")
    lines.append("")
    by_model: dict[str, list[ModelRun]] = {}
    for r in runs:
        by_model.setdefault(r.model, []).append(r)
    any_multi = False
    for model, mruns in by_model.items():
        if len(mruns) < 2:
            continue
        any_multi = True
        mruns = sorted(mruns, key=lambda x: x.run_index)
        a, b = mruns[0], mruns[1]
        stats = _overlap_stats(a.wiki_pages, b.wiki_pages)
        lines.append(f"### {model}: run 0 vs run 1")
        lines.append("")
        lines.append(f"- Jaccard overlap: **{stats['jaccard']:.2f}** "
                     f"({len(stats['both'])}/{stats['union_size']} pages)")
        lines.append(f"- Only in run 0: {stats['only_a'] or 'none'}")
        lines.append(f"- Only in run 1: {stats['only_b'] or 'none'}")
        lines.append(f"- In both: {stats['both'] or 'none'}")
        lines.append("")
    if not any_multi:
        lines.append("_(no model was run more than once — pass `--runs 2` or "
                     "higher to enable self-consistency analysis)_")
        lines.append("")

    # ---- 3. Cross-model divergence (first run of each model)
    lines.append("## 3. Cross-model divergence (run 0 only)")
    lines.append("")

    def _first_run(model_name: str) -> ModelRun | None:
        candidates = [r for r in runs if r.model == model_name]
        if not candidates:
            return None
        candidates.sort(key=lambda x: x.run_index)
        return candidates[0]

    flash = _first_run("gemini-2.5-flash")
    pro25 = _first_run("gemini-2.5-pro")
    pro31 = _first_run("gemini-3.1-pro-preview")

    def _render_pair(label: str, a: ModelRun | None, b: ModelRun | None) -> None:
        if not a or not b:
            lines.append(f"### {label}")
            lines.append("")
            lines.append("_(skipped — one or both models missing from run)_")
            lines.append("")
            return
        stats = _overlap_stats(a.wiki_pages, b.wiki_pages)
        lines.append(f"### {label}")
        lines.append("")
        lines.append(f"- Jaccard: **{stats['jaccard']:.2f}** "
                     f"({len(stats['both'])}/{stats['union_size']} pages)")
        lines.append(f"- Only `{a.model}`: {stats['only_a'] or 'none'}")
        lines.append(f"- Only `{b.model}`: {stats['only_b'] or 'none'}")
        lines.append(f"- Both: {stats['both'] or 'none'}")
        lines.append("")

    _render_pair("Flash vs 2.5 Pro", flash, pro25)
    _render_pair("2.5 Pro vs 3.1 Pro", pro25, pro31)

    # ---- 4. Thoughts side-by-side
    lines.append("## 4. Thoughts side-by-side")
    lines.append("")
    for r in runs:
        thoughts = (r.parsed.get("thoughts") or "").strip()
        lines.append(f"### {r.model} (run {r.run_index})")
        lines.append("")
        if not thoughts:
            lines.append("> _(no thoughts — parse error or empty response)_")
        else:
            for line in thoughts.split("\n"):
                lines.append(f"> {line}")
        lines.append("")

    # ---- 5. Wiki update details
    lines.append("## 5. Wiki update details")
    lines.append("")
    lines.append("| Model | Run | Action | Page | Reason |")
    lines.append("|---|---|---|---|---|")
    for r in runs:
        updates = r.parsed.get("wiki_updates") or []
        if not updates:
            lines.append(f"| {r.model} | {r.run_index} | — | _(none)_ | — |")
            continue
        for u in updates:
            action = u.get("action", "update")
            page = f"{u.get('category', '?')}/{u.get('slug', '?')}"
            reason = _truncate(u.get("reason", ""), 120)
            lines.append(
                f"| {r.model} | {r.run_index} | {action} | `{page}` | {reason} |"
            )
    lines.append("")

    # ---- 6. Summary line
    lines.append("## 6. Summary")
    lines.append("")

    def _run_cost(r: ModelRun) -> float:
        return float(r.data.get("estimated_cost_usd") or 0.0)

    first_runs = [r for r in runs if r.run_index == 0]
    ok_costs = [(r, _run_cost(r)) for r in first_runs if r.ok]
    summary_bits: list[str] = []
    if ok_costs:
        cheapest = min(ok_costs, key=lambda x: x[1])
        priciest = max(ok_costs, key=lambda x: x[1])
        summary_bits.append(
            f"Cheapest parsed: **{cheapest[0].model}** "
            f"(${cheapest[1]:.4f})."
        )
        summary_bits.append(
            f"Most expensive: **{priciest[0].model}** (${priciest[1]:.4f})."
        )
        if cheapest[1] > 0:
            ratio = priciest[1] / cheapest[1]
            summary_bits.append(f"Cost ratio: **{ratio:.1f}×**.")

    # Pro vs Pro self-consistency: pick a pro model with >=2 runs.
    pro_sc_bit = None
    for model in ("gemini-2.5-pro", "gemini-3.1-pro-preview"):
        mruns = [r for r in runs if r.model == model]
        if len(mruns) >= 2:
            mruns = sorted(mruns, key=lambda x: x.run_index)
            s = _overlap_stats(mruns[0].wiki_pages, mruns[1].wiki_pages)
            pro_sc_bit = (
                f"{model} self-consistency: "
                f"**{len(s['both'])}/{s['union_size']}** pages overlap "
                f"(Jaccard {s['jaccard']:.2f})."
            )
            break
    if pro_sc_bit:
        summary_bits.append(pro_sc_bit)

    if flash and pro25:
        s = _overlap_stats(flash.wiki_pages, pro25.wiki_pages)
        summary_bits.append(
            f"Flash vs 2.5 Pro divergence: "
            f"**{len(s['both'])}/{s['union_size']}** pages overlap "
            f"(Jaccard {s['jaccard']:.2f})."
        )

    if summary_bits:
        lines.append(" ".join(summary_bits))
    else:
        lines.append("_(not enough successful runs to summarize)_")
    lines.append("")

    return "\n".join(lines)


def cmd_report(args) -> None:
    if args.run:
        run_dir = EVAL_DIR / args.run
        if not run_dir.exists() and Path(args.run).exists():
            run_dir = Path(args.run)
    else:
        latest = _latest_eval_dir()
        if latest is None:
            raise SystemExit(
                f"no eval runs found under {EVAL_DIR}. "
                f"Run `reflection_eval.py compare` first."
            )
        run_dir = latest

    if not run_dir.exists():
        raise SystemExit(f"run dir does not exist: {run_dir}")

    report_md = _build_report(run_dir)
    out_path = run_dir / "report.md"
    out_path.write_text(report_md)
    print(report_md)
    print(f"\n--- report saved to {out_path} ---")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Reflection pass A/B evaluation harness (multi-model)."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_cap = sub.add_parser("capture", help="Snapshot reflection inputs to a fixture")
    p_cap.set_defaults(func=cmd_capture)

    p_cmp = sub.add_parser("compare", help="Run a fixture through multiple models")
    p_cmp.add_argument(
        "--fixture",
        default=None,
        help="Path to fixture JSON (default: latest in ~/.deja/reflection_fixtures/)",
    )
    p_cmp.add_argument(
        "--models",
        default=DEFAULT_MODELS,
        help=f"Comma-separated model ids (default: {DEFAULT_MODELS})",
    )
    p_cmp.add_argument(
        "--runs",
        type=int,
        default=1,
        help="Runs per model (for self-consistency)",
    )
    p_cmp.add_argument(
        "--dry-run",
        action="store_true",
        help="Short-circuit the model call and emit a synthetic response "
             "(for smoke-testing the harness without cloud costs).",
    )
    p_cmp.add_argument(
        "--wiki-mode",
        choices=("full", "retrieval"),
        default="full",
        help="How wiki_text is built: 'full' uses the fixture's stored "
             "render_for_prompt() blob (production default). 'retrieval' "
             "replaces it with index.md + QMD top-N page bodies.",
    )
    p_cmp.add_argument(
        "--index-max-lines",
        type=int,
        default=None,
        help="For --wiki-mode retrieval: cap index.md at N lines "
             "(default: uncapped).",
    )
    p_cmp.add_argument(
        "--run-suffix",
        default="",
        help="Suffix appended to each per-model output filename so "
             "repeated invocations into the same run dir don't clobber "
             "each other (e.g. '_selfconsist' for a second 3.1 Pro run).",
    )
    p_cmp.add_argument(
        "--run-dir",
        default=None,
        help="Write into this existing run directory instead of creating "
             "a fresh timestamped one (used to batch multiple conditions "
             "into one report).",
    )
    p_cmp.set_defaults(func=cmd_compare)

    p_rep = sub.add_parser("report", help="Build a markdown report from a compare run")
    p_rep.add_argument(
        "--run",
        default=None,
        help="Run timestamp (directory name under ~/.deja/reflection_eval/) "
             "or absolute path. Default: latest.",
    )
    p_rep.set_defaults(func=cmd_report)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
