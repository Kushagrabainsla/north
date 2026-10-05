"""Tool ABC hierarchy. See README Section 7 and docs/CODING_STYLE.md Sections 7.3 and 16.1."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from tools.models import ToolInput, ToolOutput

if TYPE_CHECKING:
    from approval.approvals import Approvals, Decision, Request

# Arguments north adds to every call itself. Left out of a call's description so
# "did you approve this before?" matches the same call in a later task.
_INJECTED_PARAMS = frozenset({"task_id"})


class Tool(ABC):
    """Base class for every tool an agent can call.

    Subclasses set the class-level `name` and `description` strings and
    implement `run()`; a tool whose calls change something may also override
    `describe()`. Callers use `execute()`, which puts every such call to the
    approval layer before `run()` - so no tool can forget to ask.
    """

    name: str
    description: str
    # The approval layer, bound by `ToolRegistry`. Unbound, a call that would
    # need asking is refused - see `Approvals.unbound`.
    approvals: Approvals | None = None
    # Whether running this tool mutates the filesystem or external state. The
    # agent loop runs read-only tools concurrently but serializes mutating ones
    # so two edits to the same file can't race (lost update). Default False;
    # mutating tools opt in (OCP - no central switch-on-name).
    is_mutating: bool = False
    # A mutating tool that takes the workspace lock itself for the one step that needs it (the landing step of
    # `coding_agent`). The agent loop must not hold that lock around such a tool: the lock is not re-entrant, so
    # the tool would wait for the loop that is waiting for it.
    locks_workspace_itself: bool = False
    # Override in subclasses with an OpenAI-compatible JSON Schema for the
    # function parameters.  The default accepts any key/value object.
    parameters_schema: dict = {
        "type": "object",
        "additionalProperties": True,
    }

    def schema(self) -> dict:
        """Return an OpenAI-format function definition for use with tool calling."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters_schema,
            },
        }

    def mutates(self, params: dict[str, Any] | None = None) -> bool:
        """Return whether this particular call can change local or external state.

        Most tools have one permission shape and use the class-level
        ``is_mutating`` flag. Mixed tools such as the browser can override this
        method so a read/inspect call stays read-only while click/fill actions
        receive sequencing, recovery, and approval treatment as mutations.
        """
        return self.is_mutating

    async def execute(self, input: ToolInput) -> ToolOutput:
        """Run this call on an agent's behalf. Do not override.

        A call that changes something is described, decided by the approval
        layer, and only then run - with the approved `Request` on `input.approved`
        so `run()` acts on exactly what was approved.
        """
        if not self.mutates(input.params):
            return await self.run(input)
        request = await self.describe(input)
        if request is None:
            return await self.run(input)
        approvals = self._approvals()
        task_id = input.params.get("task_id")
        decision = await approvals.decide(request, task_id=task_id)
        if not decision.allowed:
            return _refused(decision, request)
        output = await self.run(input.model_copy(update={"approved": request}))
        if output.success:
            approvals.completed(request, task_id)
        return output

    def _approvals(self) -> Approvals:
        from approval.approvals import Approvals

        return self.approvals or Approvals.unbound()

    async def describe(self, input: ToolInput) -> Request | None:
        """What this call would do, for the approval layer; ``None`` when it will not act.

        The default describes the call by its name and arguments. A tool that
        knows more - that a command is read-only, what an edit changes - says so.
        """
        from approval.approvals import Request
        from approval.policy import Action, ActionKind

        args = json.dumps(
            {k: v for k, v in input.params.items() if k not in _INJECTED_PARAMS}, sort_keys=True, default=str
        )
        return Request(
            action=Action(agent=self.name, kind=ActionKind.OTHER, summary=f"{self.name} {args}", args=args),
            title=f"{self.name.replace('_', ' ').title()} - Approval Required",
            message=f"```json\n{args}\n```",
        )

    @abstractmethod
    async def run(self, input: ToolInput) -> ToolOutput:
        """Execute the tool against `input.params`. Must not raise on
        recoverable errors - return `ToolOutput(success=False, error=...)`
        instead so the `ConfidenceTracker` can record the outcome.

        Only `execute()` may call this on an agent's behalf: `run()` itself
        asks no one."""

    def format_output(self, data: dict[str, Any]) -> str:
        """Render a successful ToolOutput.data as a human-readable string.

        The default falls back to compact JSON. Subclasses override to produce
        domain-appropriate text (e.g. WriteFileTool returns a one-liner with
        the path and byte count). The Orchestrator calls this instead of
        maintaining a central switch-on-tool-name.
        """
        return json.dumps(data, indent=2) if data else "Done."


def prepared(input: ToolInput) -> Any:
    """What `describe()` computed for this call, if the call was approved through `execute()`."""
    return input.approved.prepared if input.approved is not None else None


def _refused(decision: Decision, request: Request) -> ToolOutput:
    """What the agent is told when a call is not allowed - and whether anyone was asked.

    A refusal is not the tool malfunctioning (``failure_kind="refused"``).
    """
    if decision.status is None:
        blocked = f"Blocked: {decision.reason}. This was refused outright and never shown to the user."
        return ToolOutput(success=False, failure_kind="refused", error=f"{blocked} {request.refused_hint}".strip())
    return ToolOutput(success=False, failure_kind="refused", error=request.declined)


class AuthenticatedTool(Tool, ABC):
    """A tool that requires verifying credentials before use."""

    @abstractmethod
    async def validate_credentials(self) -> bool:
        """Return True if stored credentials are valid for this tool."""


class CacheableTool(Tool, ABC):
    """A tool whose results can be cached by key."""

    @abstractmethod
    async def get_cached(self, key: str) -> ToolOutput | None:
        """Return a previously cached result for `key`, or None on miss."""

    @abstractmethod
    async def set_cached(self, key: str, result: ToolOutput) -> None:
        """Store `result` under `key`."""
