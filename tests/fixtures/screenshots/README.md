# Vision Golden Fixtures

Canonical screenshots + expected-entity specs for regression-testing the
vision prompt and grounding behavior.

## How to use

1. Take a screenshot of something you want the model to handle well
   (Gmail inbox showing a specific sender, a Zillow listing, a VS Code
   file open to `monitor/loop.py`, a Shopify theme customizer, etc.).
2. Save it here as `<short-slug>.png`.
3. Add an entry to `spec.yaml` with:
   - `file`: the PNG filename
   - `description`: one-line human note (not shown to the model)
   - `must_contain`: list of case-insensitive substrings the output MUST
     include (brand, filename, sender, wiki slug, etc.)
   - `must_not_contain`: optional, for catching hallucinations
   - `wiki_match`: optional, the `[[slug]]` the model should flag if the
     wiki is grounded properly

## Running

Network test, gated behind the `vision` marker so it doesn't run by
default (hits Gemini, costs pennies, requires GEMINI_API_KEY):

    ./venv/bin/python -m pytest tests/test_vision_fixtures.py -m vision -v

Run it:
- Before changing `wiki/prompts/vision.md`
- After any Gemini SDK upgrade
- Before a model switch (e.g. flipping `CYCLE_MODEL` to a new version)
- Quarterly as a drift check

## What this catches

- Prompt regressions (the model stops naming brands/files/people)
- Grounding collapse (the model stops using `[[slug]]` for obvious matches)
- Hallucinations (the model invents entities not on screen)
- Model swaps that silently degrade specificity
