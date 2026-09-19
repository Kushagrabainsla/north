"""Tests for resumable declarative flow execution."""

from __future__ import annotations

from approval import ApprovalDecision
from flows.registry import FlowRegistry
from flows.store import FlowRunStore
from tools.base import Tool
from tools.models import ToolInput, ToolOutput
from tools.registry import ToolRegistry
from tools.universal.flow_runner import FlowRunner
from tools.universal.run_flow import RunFlowTool


class EchoTool(Tool):
    name = "echo"
    description = "Return a value."

    async def run(self, input: ToolInput) -> ToolOutput:
        return ToolOutput(success=True, data={"value": input.params.get("value", "")})


class MutatingTool(Tool):
    name = "mutate"
    description = "Pretend to mutate something."
    is_mutating = True

    async def run(self, input: ToolInput) -> ToolOutput:
        return ToolOutput(success=True, data={"changed": True})


class ApprovingInteraction:
    async def request_approval_status(self, **kwargs):
        return ApprovalDecision.APPROVED


def _flow(tmp_path, text: str):
    flow_dir = tmp_path / "flows" / "demo"
    flow_dir.mkdir(parents=True)
    (flow_dir / "FLOW.yaml").write_text(text, encoding="utf-8")
    return FlowRegistry(tmp_path / "flows")


async def test_runner_executes_steps_and_persists_outputs(tmp_path):
    registry = _flow(
        tmp_path,
        """name: demo
description: Demo flow
steps:
  - name: first
    tool: echo
    approval: never
    params:
      value: hello
  - name: second
    tool: echo
    approval: never
    params:
      value: ${steps.first.value}
""",
    )
    tools = ToolRegistry()
    tools.register(EchoTool())
    runner = FlowRunner(registry, tools, FlowRunStore(tmp_path / "runs.db"))

    run = await runner.run("demo", inputs={})

    assert run.status == "completed"
    assert run.current_step == 2
    assert [item["data"]["value"] for item in run.outputs] == ["hello", "hello"]


async def test_runner_pauses_before_approval_and_resumes(tmp_path):
    registry = _flow(
        tmp_path,
        """name: demo
description: Demo flow
steps:
  - name: first
    tool: echo
    approval: never
    params:
      value: ready
  - name: second
    tool: echo
    approval: always
  - name: third
    tool: echo
    approval: never
""",
    )
    tools = ToolRegistry()
    tools.register(EchoTool())
    runner = FlowRunner(registry, tools, FlowRunStore(tmp_path / "runs.db"))

    paused = await runner.run("demo")
    resumed = await runner.run("demo", run_id=paused.run_id)

    assert paused.status == "paused"
    assert paused.current_step == 1
    assert resumed.status == "paused"
    assert resumed.current_step == paused.current_step
    assert len(resumed.outputs) == 1


async def test_runner_rejects_unapproved_mutation(tmp_path):
    registry = _flow(
        tmp_path,
        """name: demo
description: Demo flow
steps:
  - name: mutate
    tool: mutate
    approval: never
""",
    )
    tools = ToolRegistry()
    tools.register(MutatingTool())
    runner = FlowRunner(registry, tools, FlowRunStore(tmp_path / "runs.db"))

    run = await runner.run("demo")

    assert run.status == "needs_approval"
    assert "cannot run" in run.error


async def test_runner_surfaces_approval_and_continues_when_approved(tmp_path):
    registry = _flow(
        tmp_path,
        """name: demo
description: Demo flow
steps:
  - name: approve-me
    tool: echo
    approval: always
""",
    )
    tools = ToolRegistry()
    tools.register(EchoTool())
    runner = FlowRunner(registry, tools, FlowRunStore(tmp_path / "runs.db"), ApprovingInteraction())

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
    tool: echo
    approval: never
""",
    )
    tools = ToolRegistry()
    tools.register(EchoTool())
    tool = RunFlowTool(FlowRunner(registry, tools, FlowRunStore(tmp_path / "runs.db")))

    out = await tool.run(ToolInput(params={"name": "demo"}))

    assert out.success
    assert out.data["status"] == "completed"
    assert out.data["current_step"] == 1
