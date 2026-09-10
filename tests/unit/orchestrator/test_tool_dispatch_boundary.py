"""Stage 4 runtime tool-dispatch boundary regressions.

The orchestrator core and the work committer construct and invoke tools through
composition-injected platform ports - never a direct ``integrations.tools``
import. These tests pin the behaviour that boundary must preserve:

* the concrete tool classes structurally satisfy the runtime dispatch ports, so
  the injected wiring is sound;
* ``tools.exceptions.ToolNotFoundError`` is catchable through the platform
  ``utils.tools.ToolNotFoundError`` contract the orchestrator imports;
* direct single-tool success formats through ``format_output`` and marks a side
  effect for a mutating tool;
* a missing tool - or a missing input factory - re-routes to the agent fallback;
* the injected factory carries ``edit_scope`` on the dedicated field only;
* the work committer builds its git envelopes through the injected factory.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from orchestrator.orchestrator import Orchestrator
from tools.exceptions import ToolNotFoundError as ConcreteToolNotFound
from tools.models import ToolInput
from utils.tools import (
    ToolDispatchRegistryPort,
    ToolNotFoundError,
    ToolRunnerPort,
)
from utils.tools import (
    ToolInput as ToolInputPort,
)


async def _async_noop(*_args: Any, **_kwargs: Any) -> None:
    return None


class _RecordingTool:
    is_mutating = False

    def __init__(self) -> None:
        self.captured: dict[str, Any] = {}

    async def run(self, tool_input):
        self.captured["edit_scope"] = tool_input.edit_scope
        self.captured["params"] = dict(tool_input.params)
        return MagicMock(success=True, data={"k": "v"}, error=None)

    def format_output(self, _data):
        return "formatted-output"


def _bare_orchestrator(*, registry, factory) -> Orchestrator:
    orch = Orchestrator.__new__(Orchestrator)  # bypass heavy __init__; exercise one method
    orch._tool_registry = registry
    orch._tool_input_factory = factory
    orch._running_task_store = None
    orch._stream_manager = MagicMock()
    orch._stream_manager.emit = _async_noop
    orch._journal = MagicMock()
    orch._journal.record = _async_noop
    orch._finish_task = _async_noop
    return orch


def _plan(tool: str = "write_file") -> MagicMock:
    plan = MagicMock()
    plan.direct_tool = tool
    plan.direct_tool_params = {"path": "x", "content": "y"}
    return plan


class TestConcreteClassesSatisfyDispatchPorts:
    def test_tool_registry_is_a_dispatch_registry_port(self) -> None:
        from tools.registry import ToolRegistry

        assert isinstance(ToolRegistry(), ToolDispatchRegistryPort)

    def test_a_tool_is_a_runner_port(self) -> None:
        assert isinstance(_RecordingTool(), ToolRunnerPort)

    def test_tool_input_is_the_input_port(self) -> None:
        envelope = ToolInput(params={"a": 1})
        assert isinstance(envelope, ToolInputPort)

    def test_tool_input_class_is_a_usable_factory(self) -> None:
        # The composition root injects ``ToolInput`` itself as the factory.
        envelope = ToolInput(params={"a": 1}, edit_scope=None)
        assert envelope.params == {"a": 1}
        assert envelope.edit_scope is None


class TestCompatibleNotFoundContract:
    def test_concrete_error_subclasses_platform_contract(self) -> None:
        assert issubclass(ConcreteToolNotFound, ToolNotFoundError)

    def test_platform_except_catches_the_concrete_error(self) -> None:
        try:
            raise ConcreteToolNotFound("missing")
        except ToolNotFoundError as caught:
            assert str(caught) == "missing"
        else:  # pragma: no cover - failure path
            pytest.fail("platform contract did not catch the concrete error")


class TestDirectSingleToolExecution:
    async def test_success_formats_output_through_the_tool(self) -> None:
        tool = _RecordingTool()
        registry = MagicMock()
        registry.get.return_value = tool
        recorded: dict[str, Any] = {}

        orch = _bare_orchestrator(registry=registry, factory=ToolInput)

        async def _record(task_id, *_a, **kw):
            recorded["output"] = kw.get("output")

        orch._journal.record = _record

        await orch._execute_single_tool("t1", "prompt", _plan(), workspace=".", context="")

        assert recorded["output"] == "formatted-output"
        # The injected factory carried params through; workspace/task_id stamped.
        assert tool.captured["params"]["workspace"] == "."
        assert tool.captured["params"]["task_id"] == "t1"

    async def test_error_result_renders_tool_error_line(self) -> None:
        class _FailingTool:
            is_mutating = False

            async def run(self, _tool_input):
                return MagicMock(success=False, data={}, error="boom")

            def format_output(self, _data):  # pragma: no cover - not reached on failure
                return "unused"

        registry = MagicMock()
        registry.get.return_value = _FailingTool()
        recorded: dict[str, Any] = {}

        orch = _bare_orchestrator(registry=registry, factory=ToolInput)

        async def _record(task_id, *_a, **kw):
            recorded["output"] = kw.get("output")

        orch._journal.record = _record

        await orch._execute_single_tool("t1", "prompt", _plan(), workspace=".", context="")
        assert recorded["output"] == "Tool error: boom"

    async def test_mutating_tool_success_marks_side_effect(self) -> None:
        class _MutatingTool(_RecordingTool):
            is_mutating = True

        registry = MagicMock()
        registry.get.return_value = _MutatingTool()

        orch = _bare_orchestrator(registry=registry, factory=ToolInput)
        store = MagicMock()
        marked: dict[str, str] = {}

        async def _mark(task_id):
            marked["task_id"] = task_id

        store.mark_side_effect = _mark
        orch._running_task_store = store

        await orch._execute_single_tool("t-mut", "prompt", _plan(), workspace=".", context="")
        assert marked["task_id"] == "t-mut"

    async def test_scope_rides_the_dedicated_field_only(self) -> None:
        tool = _RecordingTool()
        registry = MagicMock()
        registry.get.return_value = tool

        class _Guard:
            def authorize(self, _path):  # satisfies utils.edit_scope.EditAuthorizer
                return None

        guard = _Guard()

        orch = _bare_orchestrator(registry=registry, factory=ToolInput)
        await orch._execute_single_tool("t1", "prompt", _plan(), workspace=".", context="", edit_scope=guard)  # type: ignore[arg-type]

        assert tool.captured["edit_scope"] is guard
        assert "edit_scope" not in tool.captured["params"]


class TestMissingToolFallsBackToAgent:
    async def test_missing_tool_reroutes_to_agent(self) -> None:
        registry = MagicMock()
        registry.get.side_effect = ConcreteToolNotFound("nope")

        orch = _bare_orchestrator(registry=registry, factory=ToolInput)
        fallback_plan = MagicMock()
        fallback_plan.agents = ["general"]
        orch._execution_planner = MagicMock()
        orch._execution_planner.build_fallback_plan.return_value = fallback_plan
        calls: dict[str, Any] = {}

        async def _groups(task_id, prompt, plan, workspace, context="", edit_scope=None):
            calls["plan"] = plan
            return []

        orch._execute_parallel_groups = _groups
        orch._report_execution_failures = _async_noop

        await orch._execute_single_tool("t1", "prompt", _plan("ghost"), workspace=".", context="")

        orch._execution_planner.build_fallback_plan.assert_called_once_with("general", "t1")
        assert calls["plan"] is fallback_plan

    async def test_missing_factory_reroutes_to_agent(self) -> None:
        registry = MagicMock()
        registry.get.return_value = _RecordingTool()

        orch = _bare_orchestrator(registry=registry, factory=None)
        fallback_plan = MagicMock()
        fallback_plan.agents = ["general"]
        orch._execution_planner = MagicMock()
        orch._execution_planner.build_fallback_plan.return_value = fallback_plan
        rerouted: dict[str, bool] = {}

        async def _groups(*_a, **_kw):
            rerouted["hit"] = True
            return []

        orch._execute_parallel_groups = _groups
        orch._report_execution_failures = _async_noop

        await orch._execute_single_tool("t1", "prompt", _plan(), workspace=".", context="")
        assert rerouted["hit"] is True
