"""Tests for the SKILL.md parser."""

from __future__ import annotations

import pytest

from skills.exceptions import SkillParseError
from skills.parser import parse_skill_document


def test_parses_frontmatter_and_body():
    meta, body = parse_skill_document("---\nname: x\ndescription: Use when y\n---\nStep 1\nStep 2")
    assert meta == {"name": "x", "description": "Use when y"}
    assert body == "Step 1\nStep 2"


def test_no_frontmatter_returns_whole_as_body():
    meta, body = parse_skill_document("Just a body, no frontmatter")
    assert meta == {}
    assert body == "Just a body, no frontmatter"


def test_empty_frontmatter_is_empty_dict():
    meta, body = parse_skill_document("---\n\n---\nbody")
    assert meta == {}
    assert body == "body"


def test_malformed_yaml_raises():
    with pytest.raises(SkillParseError):
        parse_skill_document("---\nname: [unclosed\n---\nbody")


def test_non_mapping_frontmatter_raises():
    with pytest.raises(SkillParseError):
        parse_skill_document("---\n- a\n- b\n---\nbody")
