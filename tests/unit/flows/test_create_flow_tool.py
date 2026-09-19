"""Tests for runtime flow creation and inspection."""

from __future__ import annotations

from flows.registry import FlowRegistry
from tools.models import ToolInput
from tools.universal.create_flow import CreateFlowTool
from tools.universal.use_flow import UseFlowTool


async def test_creates_and_reloads_flow(tmp_path):
    builtin_dir = tmp_path / "builtin"
    learned_dir = tmp_path / "learned"
    builtin_dir.mkdir()
    learned_dir.mkdir()
    registry = FlowRegistry(builtin_dir=builtin_dir, learned_dir=learned_dir)
    tool = CreateFlowTool(registry=registry, learned_dir=learned_dir)

    out = await tool.run(
        ToolInput(
            params={
                "action": "create",
                "name": "job-application-review",
                "description": "Prepare an application for user review.",
                "steps": [
                    {"name": "find", "tool": "browser", "approval": "never"},
                    {"name": "review", "tool": "request_approval", "approval": "always"},
                ],
            }
        )
    )

    assert out.success
    assert registry.get("job-application-review").step_names() == ["find", "review"]
    assert out.data["steps"] == 2


async def test_create_flow_rejects_invalid_definition(tmp_path):
    registry = FlowRegistry(tmp_path)
    tool = CreateFlowTool(registry=registry, learned_dir=tmp_path / "learned")
    out = await tool.run(
        ToolInput(
            params={
                "action": "create",
                "name": "broken",
                "description": "Broken flow",
                "steps": [{"name": "step", "tool": "browser", "approval": "unknown"}],
            }
        )
    )
    assert not out.success
    assert "approval" in out.error
    assert registry.names() == []


async def test_use_flow_returns_ordered_steps(tmp_path):
    learned = tmp_path / "learned"
    learned.mkdir()
    tool = CreateFlowTool(registry := FlowRegistry(tmp_path, learned), learned_dir=learned)
    await tool.run(
        ToolInput(
            params={
                "action": "create",
                "name": "one-two",
                "description": "Run two steps.",
                "steps": [{"name": "first", "tool": "browser"}, {"name": "second", "tool": "use_skill"}],
            }
        )
    )
    out = await UseFlowTool(registry).run(ToolInput(params={"name": "one-two"}))
    assert out.success
    assert [step["name"] for step in out.data["steps"]] == ["first", "second"]
