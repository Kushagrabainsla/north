"""The bootstrap schemas must satisfy the strict contract they declare.

Declaring ``strict`` is a promise about the schema's *shape*, not just a flag.
Pydantic does not emit that shape, so these schemas were rejected outright by the
one provider that enforces the contract and silently ignored by the rest - which
meant structured output was never actually enforced anywhere, and every
schema-enforced call to the Codex subscription failed with 400.
"""

from __future__ import annotations

import pytest

from bootstrap.schema import (
    EXTRACTED_FACTS_JSON_SCHEMA,
    UNIFIED_EXTRACTION_JSON_SCHEMA,
    USER_PROFILE_JSON_SCHEMA,
    _as_strict,
)

ALL_SCHEMAS = (
    EXTRACTED_FACTS_JSON_SCHEMA,
    USER_PROFILE_JSON_SCHEMA,
    UNIFIED_EXTRACTION_JSON_SCHEMA,
)


def _object_nodes(node):
    if isinstance(node, dict):
        if node.get("type") == "object" and "properties" in node:
            yield node
        for value in node.values():
            yield from _object_nodes(value)
    elif isinstance(node, list):
        for value in node:
            yield from _object_nodes(value)


def _ref_nodes(node):
    if isinstance(node, dict):
        if "$ref" in node:
            yield node
        for value in node.values():
            yield from _ref_nodes(value)
    elif isinstance(node, list):
        for value in node:
            yield from _ref_nodes(value)


@pytest.mark.parametrize("schema", ALL_SCHEMAS, ids=lambda s: s["name"])
class TestStrictContract:
    def test_every_object_forbids_extra_properties(self, schema: dict) -> None:
        for node in _object_nodes(schema):
            assert node.get("additionalProperties") is False

    def test_every_property_is_required(self, schema: dict) -> None:
        """Strict mode has no notion of an optional property."""
        for node in _object_nodes(schema):
            assert set(node["properties"]) == set(node.get("required", []))

    def test_no_ref_carries_sibling_keywords(self, schema: dict) -> None:
        """Pydantic puts default/description beside a $ref; strict mode rejects both."""
        for node in _ref_nodes(schema):
            assert set(node) == {"$ref"}, f"$ref has siblings: {sorted(set(node) - {'$ref'})}"

    def test_it_still_declares_itself_strict(self, schema: dict) -> None:
        assert schema["strict"] is True
        assert schema["name"]


def test_the_transform_does_not_mutate_its_input() -> None:
    """The Pydantic schema is shared; rewriting it in place would leak."""
    original = {"type": "object", "properties": {"a": {"type": "string"}}, "required": []}
    before = dict(original)
    _as_strict(original, "x")
    assert original == before


def test_nested_definitions_are_reached() -> None:
    schema = {
        "type": "object",
        "properties": {"item": {"$ref": "#/$defs/Item", "default": None}},
        "$defs": {"Item": {"type": "object", "properties": {"a": {"type": "string"}}}},
    }
    strict = _as_strict(schema, "x")
    assert strict["$defs"]["Item"]["additionalProperties"] is False
    assert strict["$defs"]["Item"]["required"] == ["a"]
    assert set(strict["properties"]["item"]) == {"$ref"}


def test_a_nullable_field_stays_nullable_when_it_becomes_required() -> None:
    """Requiring an optional field is only safe because it can still be null."""
    facts = UNIFIED_EXTRACTION_JSON_SCHEMA["$defs"]["FactItem"]
    evidence = facts["properties"]["evidence"]
    assert "evidence" in facts["required"]
    assert any(option.get("type") == "null" for option in evidence.get("anyOf", []))
