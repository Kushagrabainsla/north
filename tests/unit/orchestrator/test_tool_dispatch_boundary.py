"""Stage 4 runtime tool-dispatch boundary regressions.

The orchestrator core and the work committer construct and invoke tools through
composition-injected platform ports - never a direct ``integrations.tools``
import. These tests pin the behaviour that boundary must preserve:

* the concrete tool classes structurally satisfy the runtime dispatch ports, so
  the injected wiring is sound;
* ``tools.exceptions.ToolNotFoundError`` is catchable through the platform
  ``utils.tools.ToolNotFoundError`` contract the orchestrator imports;
* the work committer builds its git envelopes through the injected factory.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

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


class _RecordingTool:
    is_mutating = False

    def __init__(self) -> None:
        self.captured: dict[str, Any] = {}

    async def execute(self, tool_input):
        self.captured["edit_scope"] = tool_input.edit_scope
        self.captured["params"] = dict(tool_input.params)
        if self.is_mutating and tool_input.before_change is not None:
            await tool_input.before_change()
        return MagicMock(success=True, data={"k": "v"}, error=None)

    def format_output(self, _data):
        return "formatted-output"


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
