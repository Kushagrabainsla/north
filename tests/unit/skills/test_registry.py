"""Tests for SkillRegistry: discovery, validation, and built-in/learned precedence."""

from __future__ import annotations

from pathlib import Path

import pytest

from skills.exceptions import SkillNotFoundError
from skills.models import SkillSource
from skills.registry import MAX_BODY_CHARS, SkillRegistry


def _write_skill(base: Path, name: str, text: str) -> None:
    directory = base / name
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(text, encoding="utf-8")


def test_loads_valid_skill(tmp_path):
    _write_skill(tmp_path, "s1", "---\nname: s1\ndescription: Use when foo\n---\nthe body")
    registry = SkillRegistry(builtin_dir=tmp_path)
    assert registry.names() == ["s1"]
    assert registry.get("s1").source is SkillSource.BUILTIN


def test_skips_skill_missing_description(tmp_path):
    _write_skill(tmp_path, "s1", "---\nname: s1\n---\nthe body")
    assert SkillRegistry(builtin_dir=tmp_path).names() == []


def test_skips_skill_with_empty_body(tmp_path):
    _write_skill(tmp_path, "s1", "---\nname: s1\ndescription: Use when foo\n---\n")
    assert SkillRegistry(builtin_dir=tmp_path).names() == []


def test_skips_oversized_body(tmp_path):
    body = "x" * (MAX_BODY_CHARS + 1)
    _write_skill(tmp_path, "s1", f"---\nname: s1\ndescription: Use when foo\n---\n{body}")
    assert SkillRegistry(builtin_dir=tmp_path).names() == []


def test_malformed_skill_is_skipped_not_fatal(tmp_path):
    _write_skill(tmp_path, "bad", "---\nname: [unclosed\n---\nbody")
    _write_skill(tmp_path, "good", "---\nname: good\ndescription: Use when foo\n---\nbody")
    assert SkillRegistry(builtin_dir=tmp_path).names() == ["good"]


def test_learned_does_not_override_builtin(tmp_path):
    builtin, learned = tmp_path / "builtin", tmp_path / "learned"
    _write_skill(builtin, "dup", "---\nname: dup\ndescription: the built-in one\n---\nB")
    _write_skill(learned, "dup", "---\nname: dup\ndescription: the learned one\n---\nL")
    registry = SkillRegistry(builtin_dir=builtin, learned_dir=learned)
    assert registry.get("dup").description == "the built-in one"
    assert registry.get("dup").source is SkillSource.BUILTIN


def test_learned_skill_loads_with_provenance(tmp_path):
    builtin, learned = tmp_path / "builtin", tmp_path / "learned"
    builtin.mkdir()
    _write_skill(
        learned,
        "learned1",
        "---\nname: learned1\ndescription: Use when z\nsource: learned\nprovenance:\n  - task_a\n  - task_b\n---\nbody",
    )
    skill = SkillRegistry(builtin_dir=builtin, learned_dir=learned).get("learned1")
    assert skill.source is SkillSource.LEARNED
    assert skill.provenance == ("task_a", "task_b")


def test_get_unknown_raises(tmp_path):
    with pytest.raises(SkillNotFoundError):
        SkillRegistry(builtin_dir=tmp_path).get("nope")
