"""Tests for runtime flow creation and inspection."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agents.models import AgentResult
from flows.registry import FlowRegistry
from flows.store import FlowRunStore
from skills.registry import SkillRegistry
from tools.models import ToolInput
from tools.universal.create_flow import CreateFlowTool
from tools.universal.flow_runner import FlowRunner
from tools.universal.use_flow import UseFlowTool


class FakeAgent:
    name = "general"
    domain = "general"
    config = SimpleNamespace(model_pool="reasoning")

    async def run(self, payload):
        return AgentResult(output="ready", summary="done", data={"value": "ready"})


class FakeAgents:
    def get(self, name: str):
        if name != "general":
            raise KeyError(name)
        return FakeAgent()


def _skills(tmp_path) -> SkillRegistry:
    directory = tmp_path / "skills" / "review-item"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        """---
name: review-item
description: "Use when reviewing one item."
domains: [general]
execution:
  agent: general
  tools: []
  approval: never
  inputs:
    type: object
    properties: {}
    additionalProperties: true
  outputs:
    type: object
    properties: {}
    additionalProperties: true
  success_criteria:
    - A structured review result was returned.
---
# Review item

Inspect the supplied item and return a structured result.
""",
        encoding="utf-8",
    )
    return SkillRegistry(tmp_path / "skills")


def _manager(tmp_path):
    learned = tmp_path / "learned"
    registry = FlowRegistry(tmp_path / "builtin", learned)
    skills = _skills(tmp_path)
    agents = FakeAgents()
    runs = FlowRunStore(tmp_path / "runs.db")
    manager = CreateFlowTool(
        registry,
        learned,
        skill_registry=skills,
        agent_registry=agents,
        flow_store=runs,
    )
    return manager, registry, skills, agents, runs


def _steps(value: str = "ready") -> list[dict]:
    return [
        {
            "name": "review",
            "skill": "review-item",
            "instructions": "Review the supplied item.",
            "approval": "never",
            "inputs": {"value": value},
        }
    ]


async def test_creates_and_reloads_skill_based_flow(tmp_path):
    tool, registry, _skills_registry, _agents, _runs = _manager(tmp_path)

    out = await tool.run(
        ToolInput(
            params={
                "action": "create",
                "name": "job-application-review",
                "description": "Prepare an application for user review.",
                "steps": [
                    *_steps(),
                    {
                        "name": "final-check",
                        "skill": "review-item",
                        "instructions": "Check the prior review.",
                        "approval": "always",
                        "inputs": {"review": "${steps.review.output}"},
                    },
                ],
            }
        )
    )

    assert out.success
    assert registry.get("job-application-review").step_names() == ["review", "final-check"]
    assert registry.get("job-application-review").steps[0].skill == "review-item"
    assert out.data["steps"] == 2
    assert registry.get("job-application-review").status == "candidate"


async def test_create_flow_rejects_invalid_approval(tmp_path):
    tool, registry, _skills_registry, _agents, _runs = _manager(tmp_path)
    invalid = _steps()
    invalid[0]["approval"] = "unknown"

    out = await tool.run(
        ToolInput(
            params={
                "action": "create",
                "name": "broken",
                "description": "Broken flow",
                "steps": invalid,
            }
        )
    )

    assert not out.success
    assert "approval" in out.error
    assert registry.names() == []


async def test_create_flow_rejects_boolean_approval_instead_of_silently_changing_it(tmp_path):
    tool, _registry, _skills_registry, _agents, _runs = _manager(tmp_path)
    invalid = _steps()
    invalid[0]["approval"] = False

    out = await tool.run(
        ToolInput(
            params={
                "action": "create",
                "name": "unsafe-default",
                "description": "Do not reinterpret an approval boolean.",
                "steps": invalid,
            }
        )
    )

    assert not out.success
    assert "not a boolean" in out.error


async def test_create_flow_rejects_direct_tool_steps(tmp_path):
    tool, _registry, _skills_registry, _agents, _runs = _manager(tmp_path)

    out = await tool.run(
        ToolInput(
            params={
                "action": "create",
                "name": "old-shape",
                "description": "Do not create direct tool steps.",
                "steps": [{"name": "inspect", "tool": "browser", "approval": "never"}],
            }
        )
    )

    assert not out.success
    assert "reference only skills" in out.error


async def test_create_flow_checks_live_skills_and_skill_owned_executor(tmp_path):
    tool, registry, skills_registry, _agents, _runs = _manager(tmp_path)
    missing_skill = _steps()
    missing_skill[0]["skill"] = "missing"
    bad_directory = tmp_path / "skills" / "bad-executor"
    bad_directory.mkdir()
    (bad_directory / "SKILL.md").write_text(
        (tmp_path / "skills" / "review-item" / "SKILL.md")
        .read_text(encoding="utf-8")
        .replace("name: review-item", "name: bad-executor")
        .replace("agent: general", "agent: missing"),
        encoding="utf-8",
    )
    skills_registry.reload()
    missing_agent = _steps()
    missing_agent[0]["skill"] = "bad-executor"

    unknown_skill = await tool.run(
        ToolInput(
            params={
                "action": "create",
                "name": "unknown-skill",
                "description": "Reference an unavailable procedure.",
                "steps": missing_skill,
            }
        )
    )
    unknown_agent = await tool.run(
        ToolInput(
            params={
                "action": "create",
                "name": "unknown-agent",
                "description": "Reference an unavailable executor.",
                "steps": missing_agent,
            }
        )
    )

    assert not unknown_skill.success and "unknown skill" in unknown_skill.error
    assert not unknown_agent.success and "unknown skill executor" in unknown_agent.error
    assert registry.names() == []


async def test_candidate_needs_exact_successful_test_before_activation(tmp_path):
    manager, registry, skills, agents, runs = _manager(tmp_path)
    created = await manager.run(
        ToolInput(
            params={
                "action": "create",
                "name": "tested-flow",
                "description": "Only activate after the exact definition passes.",
                "steps": _steps(),
            }
        )
    )
    assert created.success

    runner = FlowRunner(registry, agents, skills, runs)
    with pytest.raises(ValueError, match="not active"):
        await runner.run("tested-flow")
    tested = await runner.run("tested-flow", test_mode=True)
    denied = await manager.run(
        ToolInput(
            params={
                "action": "activate",
                "name": "tested-flow",
                "test_run_id": tested.run_id,
                "user_confirmed": False,
            }
        )
    )
    activated = await manager.run(
        ToolInput(
            params={
                "action": "activate",
                "name": "tested-flow",
                "test_run_id": tested.run_id,
                "user_confirmed": True,
            }
        )
    )

    assert not denied.success and "confirmation" in denied.error
    assert activated.success
    assert registry.get("tested-flow").status == "active"
    assert registry.get("tested-flow").activation_fingerprint


async def test_updating_a_flow_returns_it_to_candidate_and_invalidates_old_test(tmp_path):
    manager, registry, skills, agents, runs = _manager(tmp_path)
    base = {
        "action": "create",
        "name": "changing-flow",
        "description": "A flow whose edits require new evidence.",
        "steps": _steps("before"),
    }
    assert (await manager.run(ToolInput(params=base))).success
    old_test = await FlowRunner(registry, agents, skills, runs).run("changing-flow", test_mode=True)

    updated = await manager.run(
        ToolInput(
            params={
                **base,
                "action": "update",
                "steps": _steps("after"),
            }
        )
    )
    stale_activation = await manager.run(
        ToolInput(
            params={
                "action": "activate",
                "name": "changing-flow",
                "test_run_id": old_test.run_id,
                "user_confirmed": True,
            }
        )
    )

    assert updated.success
    assert registry.get("changing-flow").status == "candidate"
    assert not stale_activation.success
    assert "changed after it was tested" in stale_activation.error


async def test_editing_a_referenced_skill_invalidates_an_activated_flow(tmp_path):
    manager, registry, skills, agents, runs = _manager(tmp_path)
    assert (
        await manager.run(
            ToolInput(
                params={
                    "action": "create",
                    "name": "skill-sensitive",
                    "description": "Track the exact procedure tested.",
                    "steps": _steps(),
                }
            )
        )
    ).success
    runner = FlowRunner(registry, agents, skills, runs)
    tested = await runner.run("skill-sensitive", test_mode=True)
    assert (
        await manager.run(
            ToolInput(
                params={
                    "action": "activate",
                    "name": "skill-sensitive",
                    "test_run_id": tested.run_id,
                    "user_confirmed": True,
                }
            )
        )
    ).success

    skill_file = tmp_path / "skills" / "review-item" / "SKILL.md"
    skill_file.write_text(skill_file.read_text(encoding="utf-8") + "\nCheck one more condition.\n", encoding="utf-8")
    skills.reload()

    with pytest.raises(ValueError, match="referenced skill was edited"):
        await runner.run("skill-sensitive")


async def test_use_flow_returns_ordered_skill_steps(tmp_path):
    manager, registry, _skills_registry, _agents, _runs = _manager(tmp_path)
    await manager.run(
        ToolInput(
            params={
                "action": "create",
                "name": "one-two",
                "description": "Run two procedures.",
                "steps": [
                    *_steps(),
                    {
                        "name": "second",
                        "skill": "review-item",
                        "instructions": "Review the first result.",
                    },
                ],
            }
        )
    )
    out = await UseFlowTool(registry).run(ToolInput(params={"name": "one-two"}))

    assert out.success
    assert [step["name"] for step in out.data["steps"]] == ["review", "second"]
    assert all("tool" not in step for step in out.data["steps"])
    assert all("agent" not in step for step in out.data["steps"])


async def test_create_flow_rejects_agent_override(tmp_path):
    tool, _registry, _skills_registry, _agents, _runs = _manager(tmp_path)
    steps = _steps()
    steps[0]["agent"] = "general"

    out = await tool.run(
        ToolInput(
            params={
                "action": "create",
                "name": "agent-override",
                "description": "Do not let flows replace skill executors.",
                "steps": steps,
            }
        )
    )

    assert not out.success
    assert "reference only skills" in out.error
