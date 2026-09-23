"""Tests for the create_skill tool."""

from __future__ import annotations

from skills.registry import SkillRegistry
from tools.models import ToolInput
from tools.universal.create_skill import CreateSkillTool


class _Selector:
    async def select(self, prompt, candidates=None):
        if "deploy" not in prompt.lower():
            return []
        return [skill for skill in candidates or [] if skill.name == "deploy-docker"]


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
    assert skill.status == "candidate"
    assert skill.domains == frozenset({"general"})
    assert skill.available_to("general") is False


async def test_missing_params_errors(tmp_path):
    registry = SkillRegistry(builtin_dir=tmp_path)
    tool = CreateSkillTool(registry=registry, learned_dir=tmp_path)

    out = await tool.run(ToolInput(params={"name": "test"}))
    assert not out.success
    assert "description" in out.error


async def test_skill_requires_selection_tests_and_confirmation_before_activation(tmp_path):
    builtin = tmp_path / "builtin"
    learned = tmp_path / "learned"
    builtin.mkdir()
    registry = SkillRegistry(builtin, learned)
    tool = CreateSkillTool(registry, learned, skill_selector=_Selector())
    created = await tool.run(
        ToolInput(
            params={
                "name": "deploy-docker",
                "description": "Use when deploying a Docker container.",
                "instructions": "1. Build the image.\n2. Run the container.\n\n## Done when\nThe service is healthy.",
            }
        )
    )
    assert created.success

    premature = await tool.run(
        ToolInput(params={"action": "activate", "name": "deploy-docker", "user_confirmed": True})
    )
    validated = await tool.run(
        ToolInput(
            params={
                "action": "validate",
                "name": "deploy-docker",
                "positive_prompts": ["Deploy this Docker service"],
                "negative_prompts": ["Review this Python function"],
            }
        )
    )
    activated = await tool.run(
        ToolInput(params={"action": "activate", "name": "deploy-docker", "user_confirmed": True})
    )

    assert not premature.success
    assert validated.success
    assert activated.success
    assert registry.get("deploy-docker").status == "active"


async def test_creates_skill_with_flow_execution_contract(tmp_path):
    registry = SkillRegistry(tmp_path / "builtin", tmp_path / "learned")
    tool = CreateSkillTool(registry, tmp_path / "learned")

    out = await tool.run(
        ToolInput(
            params={
                "name": "review-item",
                "description": "Use when reviewing one item.",
                "instructions": "1. Review the item.\n2. Return evidence.",
                "execution": {
                    "agent": "general",
                    "tools": [],
                    "approval": "never",
                    "inputs": {
                        "type": "object",
                        "properties": {"item": {"type": "string"}},
                        "required": ["item"],
                    },
                    "outputs": {
                        "type": "object",
                        "properties": {"result": {"type": "string"}},
                        "required": ["result"],
                    },
                    "success_criteria": ["The result cites the supplied item."],
                },
            }
        )
    )

    assert out.success
    execution = registry.get("review-item").execution
    assert execution is not None
    assert execution.agent == "general"
    assert execution.inputs["required"] == ["item"]
