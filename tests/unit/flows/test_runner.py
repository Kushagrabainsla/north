"""Tests for resumable skill-based flow execution."""

from __future__ import annotations

from types import SimpleNamespace

from agents.models import AgentResult
from approval import ApprovalDecision
from flows.registry import FlowRegistry
from flows.store import FlowRunStore
from skills.registry import SkillRegistry
from tools.models import ToolInput
from tools.universal.flow_runner import FlowRunner
from tools.universal.run_flow import RunFlowTool
from utils.time import localnow


class FakeAgent:
    name = "general"
    domain = "general"
    config = SimpleNamespace(model_pool="reasoning")

    def __init__(self) -> None:
        self.payloads = []

    async def run(self, payload):
        self.payloads.append(payload)
        value = "hello" if len(self.payloads) == 1 else "done"
        return AgentResult(
            output=value,
            summary=f"completed {payload.skills[0]}",
            data={"value": value},
            tools_used=["echo"],
        )


class FakeAgents:
    def __init__(self, agent: FakeAgent) -> None:
        self.agent = agent

    def get(self, name: str):
        if name != self.agent.name:
            raise KeyError(name)
        return self.agent


class ApprovingInteraction:
    async def request_approval_status(self, **kwargs):
        return ApprovalDecision.APPROVED


def _skills(tmp_path) -> SkillRegistry:
    directory = tmp_path / "skills" / "review-item"
    directory.mkdir(parents=True)
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


def _flow(tmp_path, text: str):
    flow_dir = tmp_path / "flows" / "demo"
    flow_dir.mkdir(parents=True)
    (flow_dir / "FLOW.yaml").write_text(text, encoding="utf-8")
    return FlowRegistry(tmp_path / "flows")


async def test_runner_executes_skills_and_persists_outputs(tmp_path):
    registry = _flow(
        tmp_path,
        """name: demo
description: Demo flow
steps:
  - name: first
    skill: review-item
    instructions: Review the first item.
    approval: never
    inputs:
      value: hello
  - name: second
    skill: review-item
    instructions: Review the prior result.
    approval: never
    inputs:
      value: ${steps.first.result.value}
""",
    )
    skills = _skills(tmp_path)
    agent = FakeAgent()
    runner = FlowRunner(registry, FakeAgents(agent), skills, FlowRunStore(tmp_path / "runs.db"))

    run = await runner.run("demo", inputs={})

    assert run.status == "completed"
    assert run.current_step == 2
    assert [item["skill"] for item in run.outputs] == ["review-item", "review-item"]
    assert '"value": "hello"' in agent.payloads[1].prompt
    assert agent.payloads[0].skills == ["review-item"]
    assert agent.payloads[0].allowed_tools == []
    assert agent.payloads[0].mutation_policy == "deny"
    assert not agent.payloads[0].allow_delegation


async def test_runner_pauses_before_always_approved_step_and_resumes_at_checkpoint(tmp_path):
    registry = _flow(
        tmp_path,
        """name: demo
description: Demo flow
steps:
  - name: first
    skill: review-item
    instructions: Inspect safely.
    approval: never
  - name: second
    skill: review-item
    instructions: Perform the approved operation.
    approval: always
""",
    )
    skills = _skills(tmp_path)
    agent = FakeAgent()
    runner = FlowRunner(registry, FakeAgents(agent), skills, FlowRunStore(tmp_path / "runs.db"))

    paused = await runner.run("demo")
    resumed = await runner.run("demo", run_id=paused.run_id)

    assert paused.status == "paused"
    assert paused.current_step == 1
    assert resumed.status == "paused"
    assert len(resumed.outputs) == 1


async def test_runner_maps_approval_modes_to_server_owned_mutation_policies(tmp_path):
    registry = _flow(
        tmp_path,
        """name: demo
description: Approval policies
steps:
  - name: guarded
    skill: review-item
    instructions: Change only after approval.
    approval: on_mutation
  - name: preapproved
    skill: review-item
    instructions: Run after approving the complete step.
    approval: always
""",
    )
    skills = _skills(tmp_path)
    agent = FakeAgent()
    runner = FlowRunner(
        registry,
        FakeAgents(agent),
        skills,
        FlowRunStore(tmp_path / "runs.db"),
        ApprovingInteraction(),
    )

    run = await runner.run("demo")

    assert run.status == "completed"
    assert [payload.mutation_policy for payload in agent.payloads] == ["require_approval", "allow"]


async def test_runner_refuses_to_resume_after_a_referenced_skill_changes(tmp_path):
    registry = _flow(
        tmp_path,
        """name: demo
description: Skill fingerprint
steps:
  - name: inspect
    skill: review-item
    instructions: Inspect the item.
    approval: always
""",
    )
    skills = _skills(tmp_path)
    runner = FlowRunner(registry, FakeAgents(FakeAgent()), skills, FlowRunStore(tmp_path / "runs.db"))
    paused = await runner.run("demo")

    skill_file = tmp_path / "skills" / "review-item" / "SKILL.md"
    skill_file.write_text(skill_file.read_text(encoding="utf-8") + "\nNew required behavior.\n", encoding="utf-8")
    skills.reload()

    try:
        await runner.run("demo", run_id=paused.run_id)
    except ValueError as exc:
        assert "skill" in str(exc)
        assert "changed" in str(exc)
    else:
        raise AssertionError("expected changed skill to invalidate the in-progress flow run")


async def test_runner_surfaces_approval_and_continues_when_approved(tmp_path):
    registry = _flow(
        tmp_path,
        """name: demo
description: Demo flow
steps:
  - name: approve-me
    skill: review-item
    instructions: Run the approved review.
    approval: always
""",
    )
    skills = _skills(tmp_path)
    agent = FakeAgent()
    runner = FlowRunner(
        registry,
        FakeAgents(agent),
        skills,
        FlowRunStore(tmp_path / "runs.db"),
        ApprovingInteraction(),
    )

    run = await runner.run("demo")

    assert run.status == "completed"
    assert run.current_step == 1


async def test_run_flow_tool_returns_checkpoint(tmp_path):
    registry = _flow(
        tmp_path,
        """name: demo
description: Demo flow
steps:
  - name: first
    skill: review-item
    instructions: Inspect one item.
    approval: never
""",
    )
    skills = _skills(tmp_path)
    runner = FlowRunner(
        registry,
        FakeAgents(FakeAgent()),
        skills,
        FlowRunStore(tmp_path / "runs.db"),
    )
    tool = RunFlowTool(runner)

    out = await tool.run(ToolInput(params={"name": "demo"}))

    assert out.success
    assert out.data["status"] == "completed"
    assert out.data["current_step"] == 1


async def test_a_run_records_what_started_it(tmp_path):
    registry = _flow(
        tmp_path,
        """name: demo
description: Demo flow
steps:
  - name: first
    skill: review-item
    instructions: Inspect one item.
    approval: never
""",
    )
    store = FlowRunStore(tmp_path / "runs.db")
    runner = FlowRunner(registry, FakeAgents(FakeAgent()), _skills(tmp_path), store)
    tool = RunFlowTool(runner)

    manual = await tool.run(ToolInput(params={"name": "demo"}))
    test = await tool.run(ToolInput(params={"name": "demo", "mode": "test"}))
    scheduled = await tool.run_scheduled("demo", "job-1")

    assert store.get(manual.data["run_id"]).trigger == "manual"
    assert store.get(test.data["run_id"]).trigger == "test"
    fired = store.get(scheduled.data["run_id"])
    assert fired.trigger == "schedule"
    assert fired.task_id == "job-1"


class ProducingAgent(FakeAgent):
    """An agent whose config promises a file, and which keeps or breaks the promise."""

    def __init__(self, tmp_path, *, writes: bool) -> None:
        super().__init__()
        self.target = tmp_path / "briefings" / f"{localnow().date().isoformat()}.md"
        self.config = SimpleNamespace(
            model_pool="reasoning", produces=[str(tmp_path / "briefings" / "{date}.md")]
        )
        self._writes = writes

    async def run(self, payload):
        if self._writes:
            self.target.parent.mkdir(parents=True, exist_ok=True)
            self.target.write_text("today's briefing", encoding="utf-8")
        return await super().run(payload)


_ONE_STEP = """name: demo
description: Demo flow
steps:
  - name: first
    skill: review-item
    instructions: Inspect one item.
    approval: never
"""


async def test_a_scheduled_run_fails_when_the_agent_never_wrote_its_declared_file(tmp_path):
    agent = ProducingAgent(tmp_path, writes=False)
    store = FlowRunStore(tmp_path / "runs.db")
    tool = RunFlowTool(FlowRunner(_flow(tmp_path, _ONE_STEP), FakeAgents(agent), _skills(tmp_path), store))

    out = await tool.run_scheduled("demo", "job-1")

    assert not out.success
    run = store.get(out.data["run_id"])
    assert run.status == "failed"
    assert "completed without writing" in run.error
    assert str(agent.target) in run.error


async def test_a_scheduled_run_succeeds_and_records_the_file_it_wrote(tmp_path):
    agent = ProducingAgent(tmp_path, writes=True)
    store = FlowRunStore(tmp_path / "runs.db")
    tool = RunFlowTool(FlowRunner(_flow(tmp_path, _ONE_STEP), FakeAgents(agent), _skills(tmp_path), store))

    out = await tool.run_scheduled("demo", "job-1")

    assert out.success
    assert store.get(out.data["run_id"]).outputs[0]["data"]["artifacts"] == [str(agent.target)]


async def test_a_hand_started_run_is_not_failed_for_a_missing_file(tmp_path):
    agent = ProducingAgent(tmp_path, writes=False)
    store = FlowRunStore(tmp_path / "runs.db")
    tool = RunFlowTool(FlowRunner(_flow(tmp_path, _ONE_STEP), FakeAgents(agent), _skills(tmp_path), store))

    out = await tool.run(ToolInput(params={"name": "demo"}))

    assert out.success
    assert store.get(out.data["run_id"]).outputs[0]["data"]["artifacts"] == []


async def test_an_agent_cannot_claim_its_run_was_scheduled(tmp_path):
    registry = _flow(
        tmp_path,
        """name: demo
description: Demo flow
steps:
  - name: first
    skill: review-item
    instructions: Inspect one item.
    approval: never
""",
    )
    store = FlowRunStore(tmp_path / "runs.db")
    tool = RunFlowTool(FlowRunner(registry, FakeAgents(FakeAgent()), _skills(tmp_path), store))

    out = await tool.run(ToolInput(params={"name": "demo", "trigger": "schedule"}))

    assert store.get(out.data["run_id"]).trigger == "manual"


async def test_runner_fails_when_skill_output_breaks_its_contract(tmp_path):
    registry = _flow(
        tmp_path,
        """name: demo
description: Strict output
steps:
  - name: first
    skill: review-item
    instructions: Return the required evidence.
    approval: never
""",
    )
    skills = _skills(tmp_path)
    skill_file = tmp_path / "skills" / "review-item" / "SKILL.md"
    document = skill_file.read_text(encoding="utf-8").replace(
        "  outputs:\n    type: object\n    properties: {}\n    additionalProperties: true\n",
        "  outputs:\n    type: object\n    properties:\n      evidence: {type: string}\n"
        "    required: [evidence]\n    additionalProperties: false\n",
    )
    skill_file.write_text(document, encoding="utf-8")
    skills.reload()
    runner = FlowRunner(
        registry,
        FakeAgents(FakeAgent()),
        skills,
        FlowRunStore(tmp_path / "runs.db"),
    )

    run = await runner.run("demo")

    assert run.status == "failed"
    assert "output.evidence is required" in (run.error or "")


async def test_runner_parses_json_final_response_into_contract_output(tmp_path):
    class JsonAgent(FakeAgent):
        async def run(self, payload):
            self.payloads.append(payload)
            return AgentResult(
                output='{"evidence": "verified"}',
                summary="done",
                data={"evidence_counts": {}},
            )

    registry = _flow(
        tmp_path,
        """name: demo
description: Strict output
steps:
  - name: first
    skill: review-item
    instructions: Return the required evidence.
    approval: never
""",
    )
    skills = _skills(tmp_path)
    skill_file = tmp_path / "skills" / "review-item" / "SKILL.md"
    document = skill_file.read_text(encoding="utf-8").replace(
        "  outputs:\n    type: object\n    properties: {}\n    additionalProperties: true\n",
        "  outputs:\n    type: object\n    properties:\n      evidence: {type: string}\n"
        "    required: [evidence]\n    additionalProperties: false\n",
    )
    skill_file.write_text(document, encoding="utf-8")
    skills.reload()
    runner = FlowRunner(
        registry,
        FakeAgents(JsonAgent()),
        skills,
        FlowRunStore(tmp_path / "runs.db"),
    )

    run = await runner.run("demo")

    assert run.status == "completed"
    assert run.outputs[0]["data"]["result"] == {"evidence": "verified"}


_CLEANUP_FLOW = """name: demo
description: Housekeeping
steps:
  - name: clean-up
    action: task_context_cleanup
"""


def _builtin_flow(tmp_path, text: str):
    directory = tmp_path / "builtin" / "demo"
    directory.mkdir(parents=True)
    (directory / "FLOW.yaml").write_text(text, encoding="utf-8")
    return FlowRegistry(tmp_path / "builtin")


async def test_a_system_action_step_runs_the_registered_job_with_no_agent(tmp_path):
    agent = FakeAgent()
    store = FlowRunStore(tmp_path / "runs.db")
    runner = FlowRunner(_builtin_flow(tmp_path, _CLEANUP_FLOW), FakeAgents(agent), _skills(tmp_path), store)
    calls = []

    async def cleanup() -> str:
        calls.append("ran")
        return "removed 3 stale rows"

    runner.register_action("task_context_cleanup", cleanup)
    tool = RunFlowTool(runner)

    out = await tool.run_scheduled("demo", "job-1")

    assert out.success, out.error
    assert calls == ["ran"]
    assert agent.payloads == []
    run = store.get(out.data["run_id"])
    assert run.status == "completed" and run.trigger == "schedule"
    assert run.outputs[0]["data"]["summary"] == "removed 3 stale rows"
    assert run.outputs[0]["agent"] == "system"


async def test_a_system_action_that_is_not_registered_fails_the_run_by_name(tmp_path):
    store = FlowRunStore(tmp_path / "runs.db")
    runner = FlowRunner(_builtin_flow(tmp_path, _CLEANUP_FLOW), FakeAgents(FakeAgent()), _skills(tmp_path), store)

    out = await RunFlowTool(runner).run_scheduled("demo", "job-1")

    assert not out.success
    assert "task_context_cleanup" in store.get(out.data["run_id"]).error


async def test_a_system_action_that_raises_fails_the_run_with_its_message(tmp_path):
    store = FlowRunStore(tmp_path / "runs.db")
    runner = FlowRunner(_builtin_flow(tmp_path, _CLEANUP_FLOW), FakeAgents(FakeAgent()), _skills(tmp_path), store)

    async def broken() -> str:
        raise RuntimeError("database is locked")

    runner.register_action("task_context_cleanup", broken)

    out = await RunFlowTool(runner).run_scheduled("demo", "job-1")

    assert not out.success
    assert "database is locked" in store.get(out.data["run_id"]).error
