"""CodingAgentTool - hand a coding question or plan to the user's installed coding agent.

north does not write code itself (docs/design/coding-agents.md). This runs the user's own
Claude Code, read-only: it reads the repository and answers, and cannot change anything. The
run goes through the approval layer like any other action, and the folder it reads is the one
the server granted the task, never one the model names.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from approval.approvals import Request
from approval.policy import Action, ActionKind
from coding_agents import BackendUnavailableError, CodingRunner, RunOutcome
from context.repo_instructions import load_repo_instructions
from tools.base import Tool, prepared
from tools.models import ToolInput, ToolOutput
from utils.prompts import load_prompt

_MAX_OUTPUT_CHARS = 30_000
_TITLE_CHARS = 160
_NO_FOLDER = "No folder was granted to this task, so there is nothing for a coding agent to read."


@dataclass(frozen=True)
class _Call:
    """A checked call: what to ask, where, and of which agent."""

    task: str
    workspace: str
    backend: str | None


class CodingAgentTool(Tool):
    """Ask the installed coding agent to investigate or plan, read-only."""

    name = "coding_agent"
    is_mutating = True  # it starts a process and spends the user's agent quota, so the user's mode decides
    description = (
        "Hand a coding question or a plan to the coding agent installed on this machine (Claude Code). "
        "It reads the repository and answers; it cannot change anything. Give it the whole task with the "
        "context it needs. Use it to investigate unfamiliar code, plan a change or review an approach."
    )

    def __init__(
        self,
        runner: CodingRunner,
        instructions: Callable[[str], Awaitable[str]] = load_repo_instructions,
    ) -> None:
        self._runner = runner
        self._instructions = instructions
        self.parameters_schema = {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "What to investigate or plan, with the context it needs"},
                "backend": {
                    "type": "string",
                    "enum": list(runner.names),
                    "description": "Which agent to ask; the first one available when omitted",
                },
            },
            "required": ["task"],
        }

    async def describe(self, input: ToolInput) -> Request | None:
        call = self._prepare(input)
        if isinstance(call, ToolOutput):
            return None
        who = call.backend or "the coding agent"
        summary = f"Ask {who} (read-only) in {call.workspace}: {call.task[:_TITLE_CHARS]}"
        return Request(
            action=Action(
                agent=self.name,
                kind=ActionKind.OTHER,
                summary=summary,
                # The identity of the action for learned answers: which agent, in which folder.
                operation="plan",
                args=call.backend or "",
                path=Path(call.workspace),
                workspace=call.workspace,
                details=call.task,
            ),
            title="Coding Agent - Approval Required",
            message=f"Ask **{who}** to read `{call.workspace}` (it cannot change anything):\n\n{call.task}",
            declined="Coding agent run rejected by user.",
            refused_hint="Answer from the files you can read yourself.",
            prepared=call,
        )

    async def run(self, input: ToolInput) -> ToolOutput:
        call = prepared(input) or self._prepare(input)
        if isinstance(call, ToolOutput):
            return call
        guidance = load_prompt("prompts/coding_agent_plan.md").format(
            repo_instructions=await self._instructions(call.workspace) or "(none)"
        )
        try:
            report = await self._runner.run(
                task_id=str(input.params.get("task_id") or ""),
                task=call.task,
                workspace=call.workspace,
                guidance=guidance,
                backend=call.backend,
            )
        except BackendUnavailableError as exc:
            return ToolOutput(success=False, error=str(exc))
        return _output(report.run_id, report.backend, report.outcome)

    def format_output(self, data: dict) -> str:
        text = str(data.get("answer") or "")
        denied = data.get("denied") or []
        if denied:
            tried = "; ".join(f"{item['tool']} {item['detail']}".strip() for item in denied[:5])
            text += f"\n\n(The agent tried {len(denied)} thing(s) a read-only run does not allow: {tried}.)"
        return text[:_MAX_OUTPUT_CHARS] or "The coding agent returned no answer."

    @staticmethod
    def _prepare(input: ToolInput) -> _Call | ToolOutput:
        """Check the call. The folder is the one the server granted; whatever the model named is ignored."""
        task = str(input.params.get("task", "")).strip()
        if not task:
            return ToolOutput(success=False, error="Give the coding agent a task.")
        if not input.granted_workspace:
            return ToolOutput(success=False, error=_NO_FOLDER)
        backend = str(input.params.get("backend") or "").strip() or None
        return _Call(task=task, workspace=str(Path(input.granted_workspace).resolve()), backend=backend)


def _output(run_id: str, backend: str, outcome: RunOutcome) -> ToolOutput:
    data = {
        "answer": outcome.text,
        "backend": backend,
        "run_id": run_id,
        "session_id": outcome.session_id,
        "cost_usd": outcome.cost_usd,
        "turns": outcome.turns,
        "denied": [{"tool": denial.tool, "detail": denial.detail} for denial in outcome.denials],
    }
    if outcome.ok:
        return ToolOutput(success=True, data=data)
    data["failure"] = outcome.failure.value if outcome.failure else "error"
    return ToolOutput(success=False, data=data, error=outcome.error or "The coding agent run failed.")
