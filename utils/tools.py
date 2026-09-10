"""Platform-level structural contracts for the tool subsystem.

Application code (``orchestrator``) needs to *type against* two tool-layer
objects without importing the concrete classes from ``tools`` - an outward,
layer-violating dependency (integrations sits below application in
architecture/modules.yaml). These ``Protocol`` ports name exactly the surface
the orchestration layer touches, so route wiring and the planner can be typed
without reaching into ``integrations.tools``.

The concrete ``tools.registry.ToolRegistry`` and ``tools.confidence.ConfidenceTracker``
satisfy these protocols structurally; no registration or subclassing is required.
Real runtime *construction and dispatch* of tools (``ToolInput``, ``ToolRegistry``
in ``orchestrator/orchestrator.py`` and ``orchestrator/commit.py``) is deliberately
out of scope here and keeps importing the concrete classes.

See docs/CODING_STYLE.md Section 15 and architecture/modules.yaml (platform layer).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class ToolDescriptor(Protocol):
    """The single tool attribute the planner reads when summarising tools."""

    @property
    def description(self) -> str:
        """One-line, human-readable statement of what the tool does."""
        ...


@runtime_checkable
class ToolRegistryPort(Protocol):
    """The narrow read surface the planner needs from a tool registry.

    ``ExecutionPlanner`` only lists tool names and reads a tool's description to
    build the routing prompt, and resolves a single named tool to validate a
    cached ``single_tool`` plan. Construction, reload, and dispatch stay with the
    concrete ``tools.registry.ToolRegistry`` used by the orchestrator core.
    """

    def all_tool_names(self) -> set[str]:
        """Return every registered tool name (universal and specialized)."""
        ...

    def get(self, name: str) -> ToolDescriptor:
        """Return the tool registered under ``name`` (raises if absent)."""
        ...


@runtime_checkable
class ConfidenceTrackerPort(Protocol):
    """The confidence-score surface the HTTP layer and startup wiring use.

    ``orchestrator/api/confidence.py`` reads a per-(agent, tool) score; startup
    seeds defaults once. The concrete ``tools.confidence.ConfidenceTracker``
    carries the full learning machinery; this port names only what the
    application layer reads and writes through.
    """

    async def get_score(self, agent: str, tool: str) -> float:
        """Return the current confidence score for ``tool`` under ``agent``."""
        ...

    async def seed_defaults(self, graph: dict[str, list[str]], reliable_tools: frozenset[str]) -> None:
        """Prime scores for a freshly discovered tool graph (idempotent)."""
        ...
