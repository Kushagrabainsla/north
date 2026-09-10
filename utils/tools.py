"""Platform-level structural contracts for the tool subsystem.

Application code (``orchestrator``) needs to *type against* and *dispatch through*
tool-layer objects without importing the concrete classes from ``tools`` - an
outward, layer-violating dependency (integrations sits below application in
architecture/modules.yaml). These ``Protocol`` ports name exactly the surface the
orchestration layer touches, so route wiring, the planner, and the runtime
tool-dispatch core can be typed and driven without reaching into
``integrations.tools``.

Two families live here:

* **Type-only planner ports** (``ToolRegistryPort``, ``ToolDescriptor``,
  ``ConfidenceTrackerPort``) name the read surface the planner and HTTP wiring
  touch.
* **Runtime dispatch ports** (``ToolRunnerPort``, ``ToolDispatchRegistryPort``,
  ``ToolInput`` / ``ToolInputFactory``, ``ToolNotFoundError``) name the surface
  the orchestrator core and the work committer use to *construct and invoke*
  tools. The concrete ``ToolInput`` and ``ToolRegistry`` are injected only at the
  composition root (``orchestrator/app.py``); the runtime modules depend on these
  ports alone.

The concrete ``tools.registry.ToolRegistry``, ``tools.confidence.ConfidenceTracker``,
and ``tools.base.Tool`` satisfy these protocols structurally; no registration or
subclassing is required. The concrete ``tools.exceptions.ToolNotFoundError``
subclasses the platform :class:`ToolNotFoundError` so an ``except`` in the
orchestrator catches the lower-layer error through the platform contract.

See docs/CODING_STYLE.md Section 15 and architecture/modules.yaml (platform layer).
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from utils.edit_scope import EditAuthorizer


class ToolNotFoundError(Exception):
    """Platform contract raised when a named tool is not registered.

    The concrete ``tools.exceptions.ToolNotFoundError`` subclasses this so the
    orchestrator's ``except ToolNotFoundError`` clause - typed against the port -
    catches the lower-layer error without importing ``integrations.tools``. The
    orchestrator also raises this directly when no registry is wired at all.
    """


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


@runtime_checkable
class ToolOutcome(Protocol):
    """The result surface the orchestrator reads after dispatching one tool.

    Matches ``tools.models.ToolOutput`` structurally: ``success`` decides the
    branch, ``data`` feeds ``format_output`` on success, ``error`` renders the
    failure line.
    """

    @property
    def success(self) -> bool:
        """Whether the call worked."""
        ...

    @property
    def data(self) -> dict[str, Any]:
        """Structured result payload on success."""
        ...

    @property
    def error(self) -> str | None:
        """Human-readable message on failure."""
        ...


@runtime_checkable
class ToolInput(Protocol):
    """The tool-call envelope the orchestrator constructs and hands to a tool.

    Matches ``tools.models.ToolInput`` structurally. ``edit_scope`` is a
    dedicated, server-owned field - never part of ``params`` - so a mutation
    guard reads authority from a channel the model cannot reach.
    """

    @property
    def params(self) -> dict[str, Any]:
        """The model's (untrusted) tool arguments."""
        ...

    @property
    def edit_scope(self) -> EditAuthorizer | None:
        """The task's server-owned scope, or ``None`` for unrestricted edits."""
        ...


class ToolInputFactory(Protocol):
    """Builds a :class:`ToolInput` envelope from server-controlled inputs.

    Injected at the composition root as ``tools.models.ToolInput`` itself (the
    concrete class is callable with these keyword arguments). The orchestrator
    and the work committer call it instead of importing ``integrations.tools``.
    """

    def __call__(self, *, params: dict[str, Any], edit_scope: EditAuthorizer | None = ...) -> ToolInput:
        """Return a tool-call envelope carrying ``params`` and ``edit_scope``."""
        ...


@runtime_checkable
class ToolRunnerPort(Protocol):
    """The single-tool surface the orchestrator dispatches against.

    ``tools.base.Tool`` satisfies this structurally: run it, render its output,
    and read whether it mutated state (so the task is marked with a side effect).
    """

    is_mutating: bool

    async def run(self, input: ToolInput) -> ToolOutcome:  # noqa: A002 - mirrors Tool.run signature
        """Execute the tool against ``input``."""
        ...

    def format_output(self, data: dict[str, Any]) -> str:
        """Render a successful result as human-readable text."""
        ...


@runtime_checkable
class ToolDispatchRegistryPort(Protocol):
    """The resolve-a-tool surface the orchestrator core needs at dispatch time.

    Distinct from :class:`ToolRegistryPort`, which names only the planner's read
    surface. ``tools.registry.ToolRegistry`` satisfies both. ``get`` raises the
    platform :class:`ToolNotFoundError` (through its concrete subclass) when the
    name is absent.
    """

    def get(self, name: str) -> ToolRunnerPort:
        """Return the runnable tool registered under ``name`` (raises if absent)."""
        ...
