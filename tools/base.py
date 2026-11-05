"""Tool ABC hierarchy. See README Section 7 and docs/CODING_STYLE.md Sections 7.3 and 16.1."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tools._path import target_path
from tools._schema import conform_params
from tools.models import ToolInput, ToolOutput
from utils.workspace_lock import workspace_lock

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
    # A tool that starts runs of its own (a flow's test run, `run_flow`). Those runs' steps take the lock for
    # their own changes, so holding it for the whole tool call left them waiting on their parent for ever.
    starts_runs: bool = False
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
        input, error, request = await self.prepare(input)
        if error:
            return ToolOutput(success=False, error=error)
        if not self.mutates(input.params):
            # A read changes nothing, but it can still go round a "no" given earlier in the task.
            if (said_no := self._approvals().refused_paths(self.reads(input), input.params.get("task_id"))) is not None:
                return ToolOutput(success=False, failure_kind="refused", error=f"Blocked: {said_no}.")
            return await self.run(input)
        if request is None:
            return await self._change(input)
        approvals = self._approvals()
        task_id = input.params.get("task_id")
        decision = await approvals.decide(request, task_id=task_id)
        if not decision.allowed:
            return _refused(decision, request)
        output = await self._change(input.model_copy(update={"approved": request}))
        if output.success:
            approvals.completed(request, task_id)
        return output

    async def prepare(self, input: ToolInput) -> tuple[ToolInput, str, Request | None]:
        """This call as `execute` acts on it: its inputs as declared, why it cannot run ("" when it can),
        and what it asks the approval layer (None for a call that changes nothing).

        Shared with `ToolRegistry.ask_ahead`, so a call decided ahead is the very call that then runs.
        """
        params, problems = conform_params(self.parameters_schema, input.params)
        if problems:
            takes = ", ".join(self.parameters_schema.get("properties") or {}) or "no parameters"
            return input, f"{self.name} was not called: {'; '.join(problems)}. It takes: {takes}.", None
        input = input.model_copy(update={"params": params})
        # A call that cannot run here raises no card: you would approve something that then cannot happen.
        if why := await self.unavailable(input):
            return input, why, None
        return input, "", await self.describe(input) if self.mutates(input.params) else None

    async def _change(self, input: ToolInput) -> ToolOutput:
        """`run()`, under the lock of the folder it changes, so two changes to one tree never interleave.

        The lock covers the change and nothing else. The agent loop held it for
        the whole call, approval included: a card left waiting in one session
        stalled every other session's writes to that folder (issue #76, finding
        A5), and a flow made from chat waited for ever on its own test run, which
        needed the lock its parent held (M3).
        """
        if self.locks_workspace_itself or self.starts_runs:
            return await self.run(input)
        async with workspace_lock(str(input.params.get("workspace") or input.granted_workspace or "")):
            return await self.run(input)

    async def unavailable(self, input: ToolInput) -> str:
        """Why this call cannot run on this machine now, or "" when it can.

        `execute` asks before anything else, so a call that cannot run raises no card, for every tool
        (CODING_STYLE §7.5). A tool only states what it needs, such as a model that loads or an agent
        that is installed: you approved asking a Codex that was not installed (issue #76, M22).
        """
        del input
        return ""

    async def end_task(self, task_id: str) -> None:
        """Close what this tool opened for *task_id*, once that task has ended, however it ended.

        `ToolRegistry.end_task` calls it on every tool; one that holds nothing per task does nothing.
        """
        del task_id

    def reads(self, input: ToolInput) -> tuple[Path, ...]:
        """The paths this call reads, so a "no" given earlier in the task can cover them.

        By default its ``path`` parameter, from its ``workspace``, for a tool that
        takes one; a tool that reads elsewhere says so.
        """
        if "path" not in self.parameters_schema.get("properties", {}):
            return ()
        return (target_path(str(input.params.get("path") or "."), input.params.get("workspace")),)

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
