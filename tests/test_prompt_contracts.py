"""Prompt placeholder contract tests.

Every bundled prompt has a set of ``{placeholder}`` tokens that its
Python caller is expected to fill in. When someone adds a new token
to the prompt without updating the caller (or removes a token the
caller still tries to pass), the next LLM call blows up with a
``KeyError`` or silently leaks braces into the rendered prompt.

These tests:

  1. Confirm every bundled prompt loads and formats cleanly with the
     placeholders its production caller actually passes.
  2. Fail loudly the moment someone adds a new placeholder without
     updating the caller, or vice versa.
  3. Lock in that the bundled default and the live ``~/Deja/prompts``
     copy are byte-identical so setup and health_check assumptions hold.

Intentionally no LLM calls — this tier is pure template rendering,
runs in milliseconds, and catches the single biggest class of "why
did the cycle suddenly fail at 3am" bugs.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from deja.prompts import load as load_prompt


BUNDLED_PROMPTS_DIR = (
    Path(__file__).parent.parent / "src" / "deja" / "default_assets" / "prompts"
)


# Known placeholders for every prompt. Keep this in sync with the
# corresponding Python caller. When a new prompt lands, add it here;
# if you remove a field from the prompt, remove it here too.
EXPECTED_PLACEHOLDERS: dict[str, set[str]] = {
    "integrate": {
        "user_first_name",
        "user_name",
        "user_email",
        "user_profile",
        "current_time",
        "day_of_week",
        "time_of_day",
        "contacts_text",
        "goals",
        "wiki_text",
        "signals_text",
        "open_windows",
    },
    "onboard": {
        "user_first_name",
        "user_email",
        "user_profile",
        "current_time",
        "day_of_week",
        "time_of_day",
        "contacts_text",
        "wiki_text",
        "signals_text",
    },
    "command": {
        "user_first_name",
        "current_goals",
        "relevant_pages",
        "current_time_iso",
        "user_input",
    },
    "query": {
        "user_first_name",
        "user_profile",
        "question",
        "bundle",
    },
    "dedup_confirm": {
        "pairs",
    },
    "contradict": {
        "cluster",
    },
    "goals_reconcile_confirm": {
        "open_items",
        "recent_events",
        "user_first_name",
    },
}


def _extract_placeholders(text: str) -> set[str]:
    """Return every ``{name}`` token in the text, excluding ``{{escaped}}``.

    Uses a bounded regex so JSON examples with escaped braces ``{{...}}``
    don't get counted. The prompt loader uses ``str.format``, which
    treats ``{{`` as a literal ``{``, so escaped braces are invisible
    to the caller and must be ignored here too.
    """
    # First strip escaped pairs so they don't confuse the single-brace regex
    stripped = re.sub(r"\{\{.*?\}\}", "", text, flags=re.DOTALL)
    return set(re.findall(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", stripped))


# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(EXPECTED_PLACEHOLDERS.keys()))
def test_prompt_placeholders_match_expected(name):
    """Every bundled prompt's placeholders must match the caller contract.

    If this fails after a prompt edit, either (a) the edit added a new
    token and the caller needs to be updated, or (b) the edit removed
    a token and the expected set here needs to be updated.

    No longer ``real_wiki``-gated: prompts are bundled inside the
    package, so this test reads the bundled copy directly.
    """
    bundled = BUNDLED_PROMPTS_DIR / f"{name}.md"
    assert bundled.exists(), (
        f"Bundled prompt {name}.md missing from {BUNDLED_PROMPTS_DIR}"
    )
    found = _extract_placeholders(bundled.read_text(encoding="utf-8"))
    expected = EXPECTED_PLACEHOLDERS[name]

    extra = found - expected
    missing = expected - found
    assert not extra and not missing, (
        f"{name}.md placeholder drift:\n"
        f"  unexpected new placeholders (caller doesn't pass these): {sorted(extra)}\n"
        f"  missing placeholders (caller passes but prompt doesn't use): {sorted(missing)}"
    )


@pytest.mark.parametrize("name", sorted(EXPECTED_PLACEHOLDERS.keys()))
def test_prompt_loads_and_formats_cleanly(name):
    """``load_prompt(name).format(...)`` must succeed with the expected kwargs.

    This runs the actual template renderer — catches unescaped JSON
    braces, unmatched placeholder quoting, etc. A template that passes
    the placeholder-set check above can still explode here if there's
    a stray ``{`` in a JSON example that wasn't properly escaped.

    No longer ``real_wiki``-gated: prompts are bundled inside the
    package now, not read from ``~/Deja/prompts/``.
    """
    template = load_prompt(name)
    dummy_kwargs = {key: f"(test-{key})" for key in EXPECTED_PLACEHOLDERS[name]}
    rendered = template.format(**dummy_kwargs)

    # All known placeholders must have been substituted
    for key in EXPECTED_PLACEHOLDERS[name]:
        assert f"(test-{key})" in rendered, (
            f"{name}.md placeholder {{{key}}} did not get substituted"
        )
    # No stray unescaped braces left over (apart from the substituted values)
    assert "{{" not in rendered  # escaped braces should have collapsed
    assert "}}" not in rendered


    # test_bundled_and_live_prompts_are_identical removed: prompts are
    # now bundled inside the package and loaded from default_assets/
    # directly. There is no ~/Deja/prompts/ copy to drift from.
