"""Tests for the use_skill tool."""

from __future__ import annotations

from pathlib import Path

from skills.registry import SkillRegistry
from tools.models import ToolInput
from tools.universal.use_skill import UseSkillTool


def _write_skill(base: Path, name: str, body: str = "do the thing", reference: str | None = None) -> None:
    directory = base / name
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Use when foo\n---\n{body}", encoding="utf-8"
    )
    if reference is not None:
        (directory / reference).write_text("reference content", encoding="utf-8")


async def test_loads_skill_body(tmp_path):
    _write_skill(tmp_path, "s1", body="Step 1\nStep 2")
    tool = UseSkillTool(SkillRegistry(builtin_dir=tmp_path))
    out = await tool.run(ToolInput(params={"name": "s1"}))
    assert out.success
    assert out.data["instructions"] == "Step 1\nStep 2"
    assert out.data["reference_files"] == []


async def test_lists_reference_files(tmp_path):
    _write_skill(tmp_path, "s1", reference="helper.py")
    tool = UseSkillTool(SkillRegistry(builtin_dir=tmp_path))
    out = await tool.run(ToolInput(params={"name": "s1"}))
    assert out.data["reference_files"] == ["helper.py"]


async def test_unknown_skill_errors_listing_available(tmp_path):
    _write_skill(tmp_path, "s1")
    tool = UseSkillTool(SkillRegistry(builtin_dir=tmp_path))
    out = await tool.run(ToolInput(params={"name": "ghost"}))
    assert not out.success
    assert "s1" in out.error


async def test_missing_name_errors(tmp_path):
    tool = UseSkillTool(SkillRegistry(builtin_dir=tmp_path))
    out = await tool.run(ToolInput(params={}))
    assert not out.success
