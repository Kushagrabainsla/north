"""Tests for the platform-level tool subsystem protocols.

These pin two guarantees behind moving orchestration's type-only references off
``integrations.tools``:

* the concrete ``tools`` classes structurally satisfy the platform ports, so the
  wiring that assigns them to port-typed slots is sound; and
* the orchestration modules that only *type against* tools (the planner and the
  HTTP wiring) no longer import from ``tools`` at all, while the runtime dispatch
  modules deliberately still do.
"""

from __future__ import annotations

import ast
from pathlib import Path

from utils.tools import ConfidenceTrackerPort, ToolDescriptor, ToolRegistryPort

ROOT = Path(__file__).parents[3]


class TestConcreteClassesSatisfyPorts:
    """The ports name a subset of the concrete surface, checked structurally."""

    def test_tool_registry_is_a_registry_port(self) -> None:
        from tools.registry import ToolRegistry

        assert isinstance(ToolRegistry(), ToolRegistryPort)

    def test_confidence_tracker_is_a_tracker_port(self) -> None:
        from tools.confidence import ConfidenceTracker

        # __new__ avoids DB/wiring in __init__; runtime_checkable inspects the type.
        assert isinstance(ConfidenceTracker.__new__(ConfidenceTracker), ConfidenceTrackerPort)

    def test_a_tool_satisfies_the_descriptor(self) -> None:
        from tools.base import Tool

        class _StubTool(Tool):
            name = "stub"
            description = "does nothing"

            async def run(self, input):  # type: ignore[no-untyped-def]
                raise NotImplementedError

        assert isinstance(_StubTool(), ToolDescriptor)


class TestPortsAreTypeOnlyForOrchestration:
    """The type-only orchestration modules must not import from ``tools``."""

    TYPE_ONLY_MODULES = (
        "orchestrator/router.py",
        "orchestrator/api_context.py",
        "orchestrator/api/deps.py",
    )

    # The dispatch core still constructs and invokes concrete tools; the port
    # extraction deliberately leaves these alone.
    RUNTIME_DISPATCH_MODULES = (
        "orchestrator/orchestrator.py",
        "orchestrator/commit.py",
    )

    def _imports_tools(self, relative: str) -> bool:
        tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] == "tools":
                return True
            if isinstance(node, ast.Import) and any(alias.name.split(".")[0] == "tools" for alias in node.names):
                return True
        return False

    def test_type_only_modules_do_not_import_tools(self) -> None:
        offenders = [m for m in self.TYPE_ONLY_MODULES if self._imports_tools(m)]
        assert offenders == [], f"still importing tools: {offenders}"

    def test_runtime_dispatch_modules_still_import_tools(self) -> None:
        # Guards the boundary of this refactor: if these ever stop importing
        # tools it is a real change to dispatch, not a type-only cleanup.
        missing = [m for m in self.RUNTIME_DISPATCH_MODULES if not self._imports_tools(m)]
        assert missing == [], f"runtime dispatch no longer imports tools: {missing}"
