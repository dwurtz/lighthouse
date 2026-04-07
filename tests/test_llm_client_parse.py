"""gemini._parse_json: tolerates the envelopes Flash actually emits."""

from __future__ import annotations

import pytest

from deja.llm_client import _parse_json


def test_plain_json_object():
    assert _parse_json('{"a": 1}') == {"a": 1}


def test_plain_json_array():
    assert _parse_json('[1, 2, 3]') == [1, 2, 3]


def test_markdown_fence():
    raw = '```json\n{"a": 1}\n```'
    assert _parse_json(raw) == {"a": 1}


def test_bare_fence():
    raw = '```\n{"a": 1}\n```'
    assert _parse_json(raw) == {"a": 1}


def test_preamble_then_object():
    raw = 'Here is the result:\n{"reasoning": "ok", "updates": 3}'
    assert _parse_json(raw) == {"reasoning": "ok", "updates": 3}


def test_nested_object_extracted():
    raw = 'junk {"a": 1, "nested": {"b": 2}} trailing'
    assert _parse_json(raw) == {"a": 1, "nested": {"b": 2}}


def test_invalid_raises():
    with pytest.raises(Exception):
        _parse_json("not json at all")
