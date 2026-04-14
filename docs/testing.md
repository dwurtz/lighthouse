# Testing guide

Deja has four categories of tests. Each catches a different class of regression.

| Category | Command | When to run | Network / auth |
|---|---|---|---|
| **Unit tests** (Python) | `make test` | Before every commit | No |
| **Swift regression** | `make test-swift` (runs automatically as part of `make test`) | After touching Swift or the attributedBody decoder | No |
| **Live integration** | `make test-live` | After touching `src/deja/observations/`, after `gws` upgrades, after `mlx-vlm` upgrades | Yes |
| **Manual end-to-end** | Run the app, drive it, inspect `~/.deja/observations.jsonl` and `~/Deja/events/` | After anything that touches the full pipeline | Yes |

---

## Layer 1 — Unit tests (`make test`)

Fast, offline, run on every change. Lives in `tests/*.py`, runs under `pytest`.

```bash
make test       # quiet
make test-v     # verbose
```

**What it covers:**

- `test_imports_smoke.py` — every module imports without error
- `test_prompt_contracts.py` — integrate/command/dedup prompts have every `{placeholder}` the code tries to fill
- `test_retrieval_contract.py` — wiki retriever output shape
- `test_prefilter_format.py` — triage batch JSON shape
- `test_llm_client_parse.py` — Flash-Lite response parsing
- `test_observer_dedup.py` — dedup id logic (no actual vector math)
- `test_observer_delta_params.py` — pins the gws param shapes the delta observers use (**read the file — the test itself documents which param shapes are known-broken and why**)
- `test_wiki.py`, `test_wiki_catalog.py`, `test_wiki_linkify.py` — wiki file IO, slug generation, crosslinking
- `test_goals.py`, `test_identity.py`, `test_briefing.py`, `test_audit.py` — module-specific contracts
- `test_reflect_schedule.py` — the 3x/day reflect scheduler's slot math
- `test_health_check.py` — startup check matrix

**Fixtures you should know about** (`tests/conftest.py`):

- **`isolated_home`** (autouse) — every test gets throwaway `DEJA_HOME` and `DEJA_WIKI` tmp dirs. Nothing touches your real `~/.deja` or `~/Deja`. If you need a test that DOES hit the real wiki, mark it with `@pytest.mark.real_wiki` and the fixture skips isolation.

**Philosophy:** unit tests pin contracts, not behavior. They catch "someone changed the shape" more than "does this actually work." They're the cheapest possible signal that a refactor broke something.

---

## Layer 2 — Swift regression (`make test-swift`)

Runs the iMessage `attributedBody` decoder against real chat.db blobs.

```bash
make test-swift
```

**What it does:** compiles `menubar/Sources/Services/AttributedBodyDecoder.swift` with `scripts/test_imessage_decoder.swift` into a throwaway binary, runs it, checks that 4 real blobs decode to their expected text.

**Why it exists:** iMessage on modern macOS stores message text in a binary `attributedBody` column instead of plain `text`. The previous code filtered `WHERE text != ''` and silently dropped every outbound message for a full day before the bug was noticed. This test pins the decoder against real blobs so any refactor that breaks typedstream parsing fails loudly.

**Running this is part of `make test`** — no need to run separately unless you're iterating on the decoder.

---

## Layer 3 — Live integration (`make test-live`)

Exercises the actual external APIs. **Requires auth** (`gws` authenticated, Gemini API key set). Skipped in default pytest runs.

```bash
make test-live
```

Covers two marker groups:

### `live_gws` — Google Workspace delta polling

Each observer (`email.py`, `calendar.py`, `drive.py`) uses a specific `gws` CLI param shape to call Gmail `history.list`, Calendar `events.list` with `syncToken`, and Drive `changes.list` with `pageToken`. The CLI marshals params differently from raw Google API specs, and it's easy to ship a shape that silently 400s in production while looking right in the source.

Tests in `tests/test_observer_delta_live.py`:

- `test_gmail_history_list_params_are_accepted` — hits real Gmail history API with our exact param shape. Fails if gws rejects the call.
- `test_gmail_history_types_array_form_IS_rejected` — negative test, confirms the known-broken form is still broken. If this starts passing, we can simplify.
- `test_calendar_events_list_initial_sync_works` — bootstraps with timeMin/timeMax, expects `nextSyncToken` or `nextPageToken`.
- `test_drive_get_start_page_token_works` — Drive bootstrap call.
- `test_drive_changes_list_params_are_accepted` — Drive delta call with pageToken + includeRemoved.

**Run after:**
- `brew upgrade gws` or any gws version bump
- Touching any of `src/deja/observations/{email,calendar,drive}.py`
- Debugging "no new events arriving" in observations.jsonl

### `vision` — Vision fixture tests

Gated fixtures in `tests/fixtures/screenshots/` that run real screenshots through Gemini vision and assert the output contains expected entities. Test lives in `tests/test_vision_fixtures.py` with declarative specs in `spec.yaml`.

See `tools/vision_eval.py` for the broader A/B harness used during prompt iteration.

**Run after:**
- Touching the vision prompt (`src/deja/vision_local.py` or `describe_screen.md`)
- Evaluating a new vision model
- Suspecting a vision-quality regression

---

## Layer 4 — Manual end-to-end

The three automated layers above catch the majority of regressions. But the integrate cycle, the observation pipeline, and cross-cutting behavior (OCR → labels → signals → batch → event creation) only really shakes out on a live run.

**Typical smoke test:**

1. Kill + relaunch: `pkill -9 -f Deja.app; open -a /Applications/Deja.app`
2. Send yourself a test iMessage or email
3. Wait ~6 seconds
4. Grep `~/.deja/observations.jsonl` for the signal
5. Wait for the next integrate cycle (up to ~5 min)
6. Check `grep Reasoning ~/.deja/deja.log | tail -3` and `ls ~/Deja/events/$(date +%Y-%m-%d)/`

**Fresh-install test** (documented in the `reference_fresh_install` memory): completely wipe `~/.deja/` and `~/Deja/`, reinstall Deja.app, run through setup, confirm each permission prompt, and verify backfill produces a reasonable wiki state. Do this before cutting a release.

---

## Adding new tests

### Adding a unit test

Drop a `test_<module>.py` into `tests/`. The `isolated_home` fixture runs automatically. Test functions just need `def test_...`; pytest discovers them.

If your test needs to hit the real wiki (rare), mark it:

```python
import pytest

@pytest.mark.real_wiki
def test_something_against_real_wiki():
    ...
```

### Adding a live gws test

Append to `tests/test_observer_delta_live.py` if it's a delta-polling concern, or create `tests/test_<area>_live.py` and mark every test with `pytestmark = pytest.mark.live_gws`.

Pattern: use the `_run_gws` helper in `test_observer_delta_live.py` — it parses output, skips keyring noise, and fails loudly with the command output on error.

### Adding a Swift regression test

For the attributedBody decoder specifically: append a fixture to `scripts/test_imessage_decoder.swift`'s `cases` list. Each fixture is a hex blob + expected decoded string — pull both from a real chat.db row if possible.

For other Swift code: there's no general Swift XCTest target yet. If you need one, adding it via XcodeGen is one PR (~20 lines in `project.yml` for a `bundle.unit-test` target). We deliberately haven't bothered because the Swift code is thin and mostly UI.

### Adding a markers

`pyproject.toml`'s `[tool.pytest.ini_options].markers` is the registry. `addopts = "-m 'not vision and not live_gws'"` excludes gated markers from default runs — add your new marker to both places if it's gated.

---

## Pre-commit checklist

- `make test` — runs Swift + Python unit tests. Must pass.
- If you touched `src/deja/observations/{email,calendar,drive}.py`: `make test-live` once to confirm gws still accepts the params.
- If you touched the vision prompt: `pytest -m vision -v` once.
- For big refactors: one manual smoke test (send a message, check it lands).

## When tests catch you

Every test has a failure message explaining **what the test is protecting against** and **what likely went wrong**. Read the assertion text before googling — tests in this repo are written to teach you the gotcha, not just flip red.

Example from `test_observer_delta_params.py`:

```python
assert '"historyTypes": "messageAdded"' in src, (
    "gws gmail users history list params must use historyTypes as a "
    "STRING ('messageAdded'), not a JSON array. The array form 400s "
    "with 'Invalid value at history_types' and silently breaks the "
    "Gmail delta poller."
)
```

If you see that message, the fix is one character. The test paid for itself.
