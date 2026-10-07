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
    config = SimpleNamespace()

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
    # Neither step restricts changes: each change the guarded step makes is put
    # to the approval layer by Tool.execute, and "before_step" asked once up front.
    assert [payload.mutation_policy for payload in agent.payloads] == ["allow", "allow"]


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
        self.config = SimpleNamespace(produces=[str(tmp_path / "briefings" / "{date}.md")])
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


async def test_a_scheduled_run_pauses_when_the_agent_never_wrote_its_declared_file(tmp_path):
    agent = ProducingAgent(tmp_path, writes=False)
    store = FlowRunStore(tmp_path / "runs.db")
    tool = RunFlowTool(FlowRunner(_flow(tmp_path, _ONE_STEP), FakeAgents(agent), _skills(tmp_path), store))

    out = await tool.run_scheduled("demo", "job-1")

    assert not out.success
    run = store.get(out.data["run_id"])
    assert run.status == "paused"
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


async def test_runner_pauses_when_skill_output_breaks_its_contract(tmp_path):
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

    assert run.status == "paused"
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


async def test_a_system_action_that_is_not_registered_pauses_the_run_by_name(tmp_path):
    store = FlowRunStore(tmp_path / "runs.db")
    runner = FlowRunner(_builtin_flow(tmp_path, _CLEANUP_FLOW), FakeAgents(FakeAgent()), _skills(tmp_path), store)

    out = await RunFlowTool(runner).run_scheduled("demo", "job-1")

    assert not out.success
    assert "task_context_cleanup" in store.get(out.data["run_id"]).error


async def test_a_system_action_that_raises_pauses_the_run_with_its_message(tmp_path):
    store = FlowRunStore(tmp_path / "runs.db")
    runner = FlowRunner(_builtin_flow(tmp_path, _CLEANUP_FLOW), FakeAgents(FakeAgent()), _skills(tmp_path), store)

    async def broken() -> str:
        raise RuntimeError("database is locked")

    runner.register_action("task_context_cleanup", broken)

    out = await RunFlowTool(runner).run_scheduled("demo", "job-1")

    assert not out.success
    assert "database is locked" in store.get(out.data["run_id"]).error


class FlakyAgent(FakeAgent):
    """Answers every step, except that the step named *breaks* fails the first time it runs."""

    def __init__(self, breaks: str) -> None:
        super().__init__()
        self.breaks = breaks
        self.failed_once = False

    async def run(self, payload):
        if f"step '{self.breaks}'" in payload.prompt and not self.failed_once:
            self.failed_once = True
            self.payloads.append(payload)
            raise RuntimeError("no model could serve general")
        return await super().run(payload)


_THREE_STEPS = """name: demo
description: Demo flow
steps:
  - name: gather
    skill: review-item
    instructions: Gather.
    approval: never
  - name: draft
    skill: review-item
    instructions: Draft.
    approval: never
  - name: check
    skill: review-item
    instructions: Check.
    approval: never
"""


async def test_a_step_that_errors_pauses_the_run_there_with_earlier_outputs_kept(tmp_path):
    """#33: the work of finished steps is not lost to one step's error."""
    store = FlowRunStore(tmp_path / "runs.db")
    agent = FlakyAgent(breaks="draft")
    runner = FlowRunner(_flow(tmp_path, _THREE_STEPS), FakeAgents(agent), _skills(tmp_path), store)

    run = await runner.run("demo")

    assert (run.status, run.current_step) == ("paused", 1)
    assert [item["step"] for item in run.outputs] == ["gather"]
    assert "no model could serve general" in run.error


async def test_resuming_runs_only_the_step_that_stopped_and_what_follows(tmp_path):
    store = FlowRunStore(tmp_path / "runs.db")
    agent = FlakyAgent(breaks="draft")
    runner = FlowRunner(_flow(tmp_path, _THREE_STEPS), FakeAgents(agent), _skills(tmp_path), store)
    paused = await runner.run("demo")

    done = await runner.run("demo", run_id=paused.run_id)

    assert done.status == "completed"
    assert [item["step"] for item in done.outputs] == ["gather", "draft", "check"]
    prompts = [payload.prompt for payload in agent.payloads]
    assert sum("step 'gather'" in prompt for prompt in prompts) == 1, "a finished step is not run again"
    resumed = next(p for p in prompts[2:] if "step 'draft'" in p)
    assert "running again after it stopped with: Skill step 'draft' failed" in resumed
    assert "running again" not in next(p for p in prompts if "step 'check'" in p)


async def test_a_paused_scheduled_run_is_not_reported_as_a_success(tmp_path):
    """So the scheduled job is left needing you, rather than quietly done."""
    store = FlowRunStore(tmp_path / "runs.db")
    runner = FlowRunner(_flow(tmp_path, _THREE_STEPS), FakeAgents(FlakyAgent("draft")), _skills(tmp_path), store)

    out = await RunFlowTool(runner).run_scheduled("demo", "job-1")

    assert not out.success
    assert out.data["status"] == "paused"


class OutOfModelsAgent(FakeAgent):
    """Every model is cooling down until ``models_back`` is set."""

    def __init__(self) -> None:
        super().__init__()
        self.models_back = False

    async def run(self, payload):
        if not self.models_back:
            from inference.exceptions import AllModelsRateLimitedError

            self.payloads.append(payload)
            raise AllModelsRateLimitedError(
                "No model could serve general - 1 models / 1 endpoints: 1 cooling down (60s)"
            )
        return await super().run(payload)


def _waiting_runner(tmp_path):
    store = FlowRunStore(tmp_path / "runs.db")
    agent = OutOfModelsAgent()
    return FlowRunner(_flow(tmp_path, _THREE_STEPS), FakeAgents(agent), _skills(tmp_path), store), store, agent


async def test_a_missing_model_makes_the_run_wait_not_pause(tmp_path):
    """#34: no model is not an error to look at; the run waits for one."""
    runner, _, _ = _waiting_runner(tmp_path)

    run = await runner.run("demo")

    assert (run.status, run.current_step) == ("waiting", 0)
    assert run.error.startswith("Waiting for a model: No model could serve general")


async def test_a_waiting_run_resumes_by_itself_once_it_has_waited(tmp_path):
    import asyncio

    runner, store, agent = _waiting_runner(tmp_path)
    run = await runner.run("demo")

    assert runner.resume_waiting(min_wait_seconds=3600) == [], "too soon to try again"

    agent.models_back = True
    assert runner.resume_waiting(min_wait_seconds=0) == [run.run_id]
    assert runner.resume_waiting(min_wait_seconds=0) == [], "never started twice"
    for _ in range(20):
        await asyncio.sleep(0)

    done = store.get(run.run_id)
    assert done.status == "completed"
    assert "running again after it stopped with: Waiting for a model" in agent.payloads[1].prompt


async def test_a_schedule_does_not_start_a_second_run_while_one_waits(tmp_path):
    runner, store, _ = _waiting_runner(tmp_path)
    tool = RunFlowTool(runner)
    first = await tool.run_scheduled("demo", "job-1")

    second = await tool.run_scheduled("demo", "job-2")

    assert first.success, "a waiting run resumes by itself, so its job is not left needing you"
    assert second.data["run_id"] == first.data["run_id"]
    assert "waiting" in second.data["note"]
    assert len(store.list_runs("demo")) == 1


# ── A scheduled run brings its result to the user ────────────────────────────


class RecordingInteraction:
    def __init__(self) -> None:
        self.informed: list[dict] = []

    async def inform(self, **card) -> None:
        self.informed.append(card)


class BriefingAgent(FakeAgent):
    """Writes a briefing and names the file it wrote, as a briefing skill does."""

    def __init__(self, folder, *, fail: bool = False) -> None:
        super().__init__()
        self._folder = folder
        self._fail = fail

    async def run(self, payload):
        if self._fail:
            raise RuntimeError("the search tool broke")
        path = self._folder / "2026-10-07.md"
        path.write_text("# Reading list\n\n- PWM: Personalized World Models (arXiv:2610.04920)\n", encoding="utf-8")
        return AgentResult(output="{}", summary="{}", data={"file": str(path)}, tools_used=["write_file"])


_ONE_STEP = """name: demo
description: Demo flow
steps:
  - name: brief
    skill: review-item
    instructions: Compile the briefing.
    approval: never
"""


async def test_a_scheduled_run_sends_the_user_what_it_wrote(tmp_path):
    interaction = RecordingInteraction()
    runner = FlowRunner(
        _flow(tmp_path, _ONE_STEP),
        FakeAgents(BriefingAgent(tmp_path)),
        _skills(tmp_path),
        FlowRunStore(tmp_path / "runs.db"),
        interaction,
    )

    run = await runner.run("demo", trigger="schedule")

    assert run.status == "completed"
    [card] = interaction.informed
    assert card["title"] == "demo is ready"
    assert "PWM: Personalized World Models" in card["message"]
    assert str(tmp_path / "2026-10-07.md") in card["message"]


async def test_a_scheduled_run_that_stops_says_why(tmp_path):
    interaction = RecordingInteraction()
    runner = FlowRunner(
        _flow(tmp_path, _ONE_STEP),
        FakeAgents(BriefingAgent(tmp_path, fail=True)),
        _skills(tmp_path),
        FlowRunStore(tmp_path / "runs.db"),
        interaction,
    )

    run = await runner.run("demo", trigger="schedule")

    assert run.status == "paused"
    [card] = interaction.informed
    assert card["title"] == "demo stopped" and "the search tool broke" in card["message"]


async def test_a_run_started_by_hand_is_not_announced(tmp_path):
    """Someone who started it is watching it; only a schedule runs while nobody is."""
    interaction = RecordingInteraction()
    runner = FlowRunner(
        _flow(tmp_path, _ONE_STEP),
        FakeAgents(BriefingAgent(tmp_path)),
        _skills(tmp_path),
        FlowRunStore(tmp_path / "runs.db"),
        interaction,
    )

    await runner.run("demo", trigger="manual")
    await runner.run("demo", trigger="schedule", test_mode=True)

    assert interaction.informed == []
