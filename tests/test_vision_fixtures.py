"""Golden-fixture regression tests for the vision prompt.

Each case is a real screenshot + the entities the model must name. Running
this catches:
- prompt regressions (model stops naming brands/files/people)
- grounding collapse (model stops emitting [[slug]] on obvious matches)
- hallucinations (must_not_contain)
- model swaps that silently degrade specificity

Gated behind the `vision` pytest marker so it doesn't run by default — it
hits the Gemini API and needs GEMINI_API_KEY / GOOGLE_API_KEY.

    pytest -m vision -v

Drop screenshots into tests/fixtures/screenshots/ and add entries to
spec.yaml. Missing files are skipped cleanly, so you can add fixtures one
at a time as you notice regressions in the wild.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
import yaml


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "screenshots"
SPEC_PATH = FIXTURE_DIR / "spec.yaml"


def _load_cases() -> list[dict]:
    if not SPEC_PATH.exists():
        return []
    try:
        data = yaml.safe_load(SPEC_PATH.read_text()) or {}
    except Exception:
        return []
    return data.get("cases") or []


def _have_api_key() -> bool:
    return bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))


@pytest.mark.vision
@pytest.mark.parametrize("case", _load_cases(), ids=lambda c: c.get("file", "unknown"))
def test_vision_case(case: dict):
    """Run one screenshot through the live vision API and check its output.

    Not a unit test — this is a live-network regression harness. Intended
    to be run manually before prompt changes, not on every commit.
    """
    if not _have_api_key():
        pytest.skip("GEMINI_API_KEY not set")

    filename = case.get("file")
    if not filename:
        pytest.skip("case has no 'file' field")
    img_path = FIXTURE_DIR / filename
    if not img_path.exists():
        pytest.skip(f"fixture image {filename} not present")

    # Import lazily so the module loads even when google-genai fails.
    from lighthouse.llm_client import GeminiClient

    client = GeminiClient()
    result = asyncio.run(client.describe_screen(str(img_path)))
    summary = (result.get("summary") or "").lower()

    assert summary, f"vision returned empty summary for {filename}"

    # Required substrings — specificity contract
    for needle in case.get("must_contain", []):
        assert needle.lower() in summary, (
            f"[{filename}] vision output missing required substring {needle!r}\n"
            f"---\n{summary}\n---"
        )

    # Forbidden substrings — hallucination catches
    for bad in case.get("must_not_contain", []) or []:
        assert bad.lower() not in summary, (
            f"[{filename}] vision output contains forbidden substring {bad!r}\n"
            f"---\n{summary}\n---"
        )

    # Wiki grounding check — the model should recognize the entity, either
    # via strict [[slug]] link syntax OR by naming the title in prose (with
    # dashes replaced by spaces). The test accepts either form: the semantic
    # contract is "the model recognized this entity", not "the model picked
    # a particular output format".
    #
    # Observed grounding behavior: link syntax fires reliably for short,
    # visually-anchored slugs ([[ship-new-blade-rose-theme]] on the actual
    # Shopify editor) and tends to degrade on long slugs or weak visual
    # anchors. Keeping the test liberal here prevents it from flaking on
    # that axis while still catching true grounding collapse (model fails
    # to name the entity at all).
    wiki_match = case.get("wiki_match")
    if wiki_match:
        link_form = f"[[{wiki_match}]]".lower()
        prose_form = wiki_match.replace("-", " ").lower()
        if link_form not in summary and prose_form not in summary:
            raise AssertionError(
                f"[{filename}] model failed to recognize wiki entity "
                f"{wiki_match!r} — neither {link_form!r} nor prose form "
                f"{prose_form!r} appeared in output\n---\n{summary}\n---"
            )


def test_spec_file_parses():
    """Always runs — ensures spec.yaml is valid YAML even when fixtures absent."""
    if not SPEC_PATH.exists():
        return
    data = yaml.safe_load(SPEC_PATH.read_text())
    assert isinstance(data, dict)
    cases = data.get("cases") or []
    assert isinstance(cases, list)
    for i, case in enumerate(cases):
        assert isinstance(case, dict), f"case {i} is not a dict"
        assert "file" in case, f"case {i} missing 'file'"
        assert "must_contain" in case, f"case {i} missing 'must_contain'"
        assert isinstance(case["must_contain"], list)
