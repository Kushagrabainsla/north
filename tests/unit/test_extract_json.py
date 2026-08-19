"""Unit tests for utils.text.extract_json (lenient JSON extraction)."""
from __future__ import annotations

from utils.text import extract_json


def test_bare_json() -> None:
    assert extract_json('{"a": 1}') == {"a": 1}


def test_fenced_json() -> None:
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_prose_wrapped_json() -> None:
    # Free models often emit a leading sentence before the JSON object.
    assert extract_json('Here is the plan: {"agents": ["general"]}') == {"agents": ["general"]}


def test_array() -> None:
    assert extract_json("no json here but [1, 2, 3] at end") == [1, 2, 3]


def test_nested() -> None:
    text = 'Sure! {"plan": {"steps": [1, 2]}} done'
    assert extract_json(text) == {"plan": {"steps": [1, 2]}}


def test_no_json_raises() -> None:
    import pytest

    with pytest.raises(ValueError):
        extract_json("just some plain english with no json at all")
