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
    config = SimpleNamespace()
    broken = False  # set on the class: the test run's step then fails

    async def run(self, payload):
        if FakeAgent.broken:
            raise RuntimeError("the step broke")
        return AgentResult(output="ready", summary="done", data={"value": "ready"})


class FakeAgents:
    def get(self, name: str):
        if name != "general":
            raise KeyError(name)
        return FakeAgent()


class Approving:
    """Each step's own card, answered yes."""

    async def request_approval_status(self, **_card):
        from approval import ApprovalDecision

        return ApprovalDecision.APPROVED

    def withdraw(self, **_card) -> None:
        pass


@pytest.fixture(autouse=True)
def _working_agent():
    FakeAgent.broken = False
    yield
    FakeAgent.broken = False


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
        runner=FlowRunner(registry, agents, skills, runs, Approving()),
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


def test_tool_description_requires_explicit_timing_intake() -> None:
    assert "resolve missing setup choices with ask_user" in CreateFlowTool.description
    assert "manual-only, one-time, or recurring timing (never assume)" in CreateFlowTool.description
    assert "collecting timing does not install a schedule or authorize activation" in CreateFlowTool.description


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

    assert out.success, out.error
    assert registry.get("job-application-review").step_names() == ["review", "final-check"]
    assert registry.get("job-application-review").steps[0].skill == "review-item"
    assert out.data["steps"] == 2
    assert registry.get("job-application-review").status == "active", "tested and switched on in the same call"


async def test_user_authored_flow_update_needs_fresh_consent_and_preserves_ownership(tmp_path):

    from config.approval_mode import ApprovalMode
    from policies.self_edit import SelfEditPolicy
    from tests.conftest import approving_store, bind_approvals, rejecting_store

    tool, registry, *_ = _manager(tmp_path)
    created = await tool.run(
        ToolInput(params={"action": "create", "name": "mine", "description": "Original", "steps": _steps()})
    )
    path = tool._learned_dir / "mine" / "FLOW.yaml"
    original = path.read_text()
    policy = SelfEditPolicy(tool._learned_dir, tmp_path / "mutations", user_document="FLOW.yaml")
    tool._self_edit_policy = policy
    call = ToolInput(params={"action": "update", "name": "mine", "description": "Improved", "steps": _steps()})
    assert created.success
    assert not (await tool.run(call)).success
    assert path.read_text() == original
    store = rejecting_store()
    bind_approvals(tool, ApprovalMode.YOLO, store=store)
    assert not (await tool.execute(call)).success
    store.wait_for_decision.assert_awaited_once()
    assert path.read_text() == original
    request = await tool.describe(call)
    assert request.requires_confirmation
    approved = call.model_copy(update={"approved": request})
    store = approving_store()
    bind_approvals(tool, ApprovalMode.YOLO, store=store)
    out = await tool.execute(call)
    store.wait_for_decision.assert_awaited_once()
    assert out.success and registry.get("mine").status == "active"
    [edit] = [record for record in policy._read_registry() if record["operation"] == "user_update"]
    assert policy.authorize(path, "update") is not None
    assert policy.revert(edit["id"])
    assert path.read_text() == original
    path.write_text(original + "\n# Human changed this after approval\n")
    assert not (await tool.run(approved)).success
    assert "Human changed" in path.read_text()


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


async def test_list_and_read_describe_existing_flows_without_claiming_creation(tmp_path):
    tool, _registry, *_ = _manager(tmp_path)
    created = await tool.run(
        ToolInput(params={"action": "create", "name": "existing", "description": "Review one item.", "steps": _steps()})
    )
    assert created.success
    listed = await tool.execute(ToolInput(params={"action": "list"}))
    assert listed.success
    assert tool.format_output(listed.data) == "existing - Review one item. (1 steps; active)"
    read = await tool.execute(ToolInput(params={"action": "read", "name": "existing"}))
    assert read.success
    text = tool.format_output(read.data)
    assert "active; 1 steps" in text and "skill: review-item" in text
    assert "created" not in text and "None" not in text
    assert "is live" in tool.format_output(created.data)


def test_format_an_empty_flow_list():
    assert CreateFlowTool.format_output(None, {"flows": []}) == "No flows registered."


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


async def test_a_new_flow_that_fails_its_test_stays_a_draft_until_one_passes(tmp_path):
    manager, registry, *_ = _manager(tmp_path)
    flow = {"name": "tested-flow", "description": "Only live once a test passes.", "steps": _steps()}
    FakeAgent.broken = True

    failed = await manager.run(ToolInput(params={"action": "create", **flow}))

    assert not failed.success and "stays a draft" in failed.error
    assert registry.get("tested-flow").status == "candidate"

    FakeAgent.broken = False
    activated = await manager.run(ToolInput(params={"action": "activate", "name": "tested-flow"}))

    assert activated.success, activated.error
    assert registry.get("tested-flow").status == "active"
    assert registry.get("tested-flow").activation_fingerprint


async def test_an_edit_that_fails_its_test_puts_the_live_version_back(tmp_path):
    """An edit used to take a flow offline at once, until someone tested and activated it again."""
    manager, registry, *_ = _manager(tmp_path)
    base = {"name": "changing-flow", "description": "A flow whose edits need new evidence.", "steps": _steps("before")}
    assert (await manager.run(ToolInput(params={"action": "create", **base}))).success
    live = (manager._learned_dir / "changing-flow" / "FLOW.yaml").read_text()
    FakeAgent.broken = True

    edit = await manager.run(ToolInput(params={**base, "action": "update", "steps": _steps("after")}))

    assert not edit.success and "previous version is live again" in edit.error
    assert (manager._learned_dir / "changing-flow" / "FLOW.yaml").read_text() == live
    assert registry.get("changing-flow").status == "active"


async def test_an_edit_that_passes_is_live_at_once(tmp_path):
    manager, registry, *_ = _manager(tmp_path)
    base = {"name": "changing-flow", "description": "A flow whose edits need new evidence.", "steps": _steps("before")}
    assert (await manager.run(ToolInput(params={"action": "create", **base}))).success

    edit = await manager.run(ToolInput(params={**base, "action": "update", "steps": _steps("after")}))

    assert edit.success, edit.error
    assert registry.get("changing-flow").steps[0].inputs == {"value": "after"}
    assert registry.get("changing-flow").status == "active"


async def test_an_old_test_run_cannot_switch_on_a_changed_flow(tmp_path):
    """The page's Activate takes a test run; it has to be a test of exactly this version."""
    manager, registry, skills, agents, runs = _manager(tmp_path)
    base = {"name": "changing-flow", "description": "A flow whose edits need new evidence.", "steps": _steps("before")}
    assert (await manager.run(ToolInput(params={"action": "create", **base}))).success
    old_test = runs.list_runs("changing-flow")[0]
    path = manager._learned_dir / "changing-flow" / "FLOW.yaml"
    path.write_text(path.read_text().replace("before", "after").replace("status: active", "status: candidate"))
    registry.reload()

    stale = await manager.run(
        ToolInput(params={"action": "activate", "name": "changing-flow", "test_run_id": old_test.run_id})
    )

    assert not stale.success and "changed after it was tested" in stale.error


async def test_editing_a_referenced_skill_invalidates_a_live_flow(tmp_path):
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

    skill_file = tmp_path / "skills" / "review-item" / "SKILL.md"
    skill_file.write_text(skill_file.read_text(encoding="utf-8") + "\nCheck one more condition.\n", encoding="utf-8")
    skills.reload()

    with pytest.raises(ValueError, match="referenced skill was edited"):
        await FlowRunner(registry, agents, skills, runs).run("skill-sensitive")


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
