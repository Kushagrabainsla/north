"""Tests for the create_skill tool."""

from __future__ import annotations

from skills.registry import SkillRegistry
from tools.models import ToolInput
from tools.universal.create_skill import CreateSkillTool


async def test_creates_and_reloads_skill(tmp_path):
    builtin_dir = tmp_path / "builtin"
    learned_dir = tmp_path / "learned"
    builtin_dir.mkdir()
    learned_dir.mkdir()

    registry = SkillRegistry(builtin_dir=builtin_dir, learned_dir=learned_dir)
    tool = CreateSkillTool(registry=registry, learned_dir=learned_dir)

    out = await tool.run(
        ToolInput(
            params={
                "name": "deploy-docker",
                "description": "Use when deploying a Docker container.",
                "instructions": "Step 1: docker build\nStep 2: docker run",
            }
        )
    )

    assert out.success
    assert out.data["name"] == "deploy-docker"

    # Verify skill was created on disk and registered in SkillRegistry
    skill = registry.get("deploy-docker")
    assert skill.description == "Use when deploying a Docker container."
    assert "docker build" in skill.body


async def test_missing_params_errors(tmp_path):
    registry = SkillRegistry(builtin_dir=tmp_path)
    tool = CreateSkillTool(registry=registry, learned_dir=tmp_path)

    out = await tool.run(ToolInput(params={"name": "test"}))
    assert not out.success
    assert "description" in out.error
