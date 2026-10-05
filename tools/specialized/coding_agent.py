"""CodingAgentTool - hand a coding task to the user's installed coding agent.

north does not write code itself (docs/design/coding-agents.md). This runs the user's own
Claude Code. In plan mode it only reads and answers. In edit mode it changes files in an isolated
copy of the repository, every action ruled on by the approval layer, and leaves the result on a
branch; nothing is applied to the real working tree. The run itself goes through the approval
layer too, and the folder is the one the server granted the task, never one the model names.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from approval.approvals import Request
from approval.policy import Action, ActionKind
from coding_agents import Briefing, CodingAgentError, CodingRunner, FailureKind, Mode, RunReport
from context.repo_instructions import load_repo_instructions
from tools.base import Tool, prepared
from tools.models import ToolInput, ToolOutput
from utils.prompts import load_prompt

logger = logging.getLogger(__name__)

_MAX_OUTPUT_CHARS = 30_000
_TITLE_CHARS = 160
_NO_FOLDER = "No folder was granted to this task, so there is nothing for a coding agent to read."


@dataclass(frozen=True)
class _Call:
    """A checked call: what to ask, where, and of which agent."""

    task: str
    workspace: str
    backend: str | None
    mode: Mode
    review: bool


class CodingAgentTool(Tool):
    """Ask the installed coding agent to investigate or plan, read-only."""

    name = "coding_agent"
    locks_workspace_itself = True  # its landing step takes the workspace lock; see `Tool.locks_workspace_itself`
    is_mutating = True  # it starts a process and spends the user's agent quota, so the user's mode decides
    description = (
        "Hand a coding task to the coding agent installed on this machine (Claude Code). Give it the whole "
        "task with the context it needs. mode 'plan' (the default) only reads the repository and answers: use "
        "it to investigate unfamiliar code, plan a change or review an approach. mode 'edit' makes the change "
        "in an isolated copy and leaves it on a new branch for the user to review; nothing is applied to the "
        "working tree."
    )

    def __init__(
        self,
        runner: CodingRunner,
        instructions: Callable[[str], Awaitable[str]] = load_repo_instructions,
        briefing: Briefing | None = None,
    ) -> None:
        self._runner = runner
        self._instructions = instructions
        self._briefing = briefing
        self.parameters_schema = {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "What to investigate or plan, with the context it needs"},
                "mode": {
                    "type": "string",
                    "enum": [Mode.PLAN.value, *([Mode.EDIT.value] if runner.can_edit else [])],
                    "description": "plan reads and answers (default); edit changes an isolated copy on a new branch",
                },
                "review": {
                    "type": "boolean",
                    "description": "edit only: have the other coding agent, if installed, read the change first "
                    "(default true)",
                },
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
        editing = call.mode is Mode.EDIT
        summary = f"Ask {who} ({'edit' if editing else 'read-only'}) in {call.workspace}: {call.task[:_TITLE_CHARS]}"
        what = "to change an isolated copy of" if editing else "to read"
        promise = (
            "Every action it takes is checked first, and the result is left on a new branch; "
            "nothing is applied to your working tree."
            if editing
            else "It cannot change anything."
        )
        return Request(
            action=Action(
                agent=self.name,
                kind=ActionKind.OTHER,
                summary=summary,
                # The identity of the action for learned answers: which mode, which agent, which folder.
                operation=call.mode.value,
                args=call.backend or "",
                path=Path(call.workspace),
                workspace=call.workspace,
                details=call.task,
            ),
            title="Coding Agent - Approval Required",
            message=f"Ask **{who}** {what} `{call.workspace}`. {promise}\n\n{call.task}",
            declined="Coding agent run rejected by user.",
            refused_hint="Answer from the files you can read yourself.",
            prepared=call,
        )

    async def run(self, input: ToolInput) -> ToolOutput:
        call = prepared(input) or self._prepare(input)
        if isinstance(call, ToolOutput):
            return call
        guidance = load_prompt(f"prompts/coding_agent_{call.mode.value}.md").format(
            repo_instructions=await self._instructions(call.workspace) or "(none)",
            north_context=await self._north_context(call) or "(nothing relevant)",
        )
        try:
            report = await self._runner.run(
                task_id=str(input.params.get("task_id") or ""),
                task=call.task,
                workspace=call.workspace,
                guidance=guidance,
                backend=call.backend,
                mode=call.mode,
                review=call.review,
            )
        except CodingAgentError as exc:  # no usable agent, edit not set up, not a git repository
            return ToolOutput(success=False, error=str(exc))
        return _output(report)

    async def _north_context(self, call: _Call) -> str:
        """The briefing for this task. It helps and never gates: if it cannot be had, the run goes without."""
        if self._briefing is None:
            return ""
        try:
            return await self._briefing.brief(call.task, call.workspace)
        except Exception:
            logger.warning("could not brief the coding agent; running it without north's context", exc_info=True)
            return ""

    def format_output(self, data: dict) -> str:
        text = str(data.get("answer") or "")
        if data.get("mode") == Mode.EDIT.value:
            text = _edit_summary(data) + ("\n\n" + text if text else "")
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
        try:
            mode = Mode(str(input.params.get("mode") or Mode.PLAN.value))
        except ValueError:
            return ToolOutput(success=False, error="mode must be 'plan' or 'edit'.")
        review = input.params.get("review", True) is not False
        return _Call(
            task=task, workspace=str(Path(input.granted_workspace).resolve()), backend=backend, mode=mode, review=review
        )


# These stop a run without failing the task: the session and the copy are kept for a later call.
_PAUSED_NOTE = {
    FailureKind.RESOURCE: "The run is PAUSED, not failed: this is the agent's usage limit or an outage. "
    "Its progress is kept. Tell the user, and call coding_agent again with the same task later to continue it.",
    FailureKind.AUTH: "The run is PAUSED, not failed: the agent is not logged in. "
    "Its progress is kept. Ask the user to log in to it, then call coding_agent again with the same task.",
}
_PAUSED_FAILURES = frozenset(_PAUSED_NOTE)


def _output(report: RunReport) -> ToolOutput:
    outcome = report.outcome
    data: dict = {
        "answer": outcome.text,
        "backend": report.backend,
        "run_id": report.run_id,
        "session_id": outcome.session_id,
        "cost_usd": outcome.cost_usd,
        "turns": outcome.turns,
        "denied": [{"tool": denial.tool, "detail": denial.detail} for denial in outcome.denials],
    }
    if report.change is not None:
        change = report.change
        data["mode"] = Mode.EDIT.value
        data["change"] = {
            "branch": change.tree.branch,
            "path": change.tree.path,
            "base": change.tree.base,
            "base_sha": change.tree.base_sha,
            "files": [{"path": f.path, "insertions": f.insertions, "deletions": f.deletions} for f in change.files],
            "insertions": change.insertions,
            "deletions": change.deletions,
        }
    elif report.mode is Mode.EDIT:
        data["mode"] = Mode.EDIT.value
    if report.verification is not None:
        check = report.verification
        data["verification"] = {"state": check.state.value, "command": check.command, "detail": check.detail}
    if report.review is not None:
        data["review"] = {
            "reviewer": report.review.reviewer,
            "verdict": report.review.verdict.value,
            "summary": report.review.summary,
        }
    if report.landing is not None:
        data["landing"] = {"state": report.landing.state.value, "reason": report.landing.reason}
    if report.problem:  # the agent finished, but what it did could not be saved: that is not "no changes"
        data["problem"] = report.problem
        data["failure"] = "copy_damaged"
        return ToolOutput(success=False, data=data, error=report.problem)
    if outcome.ok:
        return ToolOutput(success=True, data=data)
    data["failure"] = outcome.failure.value if outcome.failure else "error"
    error = outcome.error or "The coding agent run failed."
    if outcome.failure in _PAUSED_FAILURES:
        error = f"{error} {_PAUSED_NOTE[outcome.failure]}"
    return ToolOutput(success=False, data=data, error=error)


def _edit_summary(data: dict) -> str:
    change = data.get("change")
    if not change:
        return "The agent made no changes."
    files = "\n".join(f"- {f['path']} (+{f['insertions']} -{f['deletions']})" for f in change["files"][:20])
    size = f"{len(change['files'])} file(s), +{change['insertions']} -{change['deletions']}"
    tests = _tests_summary(data.get("verification"))
    if data.get("review"):
        tests = f"{tests} {_review_summary(data['review'])}".strip()
    landing = data.get("landing")
    if landing is None:  # not tested or offered: the copy is still there to look at
        return (
            f"The agent's changes are on branch `{change['branch']}` in an isolated copy at `{change['path']}` "
            f"({size}). Nothing was applied to the working tree. To review: "
            f"`git -C {change['path']} diff {change['base_sha']}..HEAD`.\n{files}"
        )
    if landing["state"] == "applied":
        return (
            f"The agent's changes were applied to your working tree as uncommitted changes ({size}). {tests}\n{files}"
        )
    why = {
        "conflict": "the same lines changed in your working tree meanwhile",
        "declined": "you chose to keep them on the branch",
    }.get(landing["state"], landing["reason"])
    return (
        f"The agent's changes were NOT applied: {why}. {tests} They are on branch `{change['branch']}` "
        f"in your repository ({size}). To review: "
        f"`git -C {change['base']} diff {change['base_sha']}..{change['branch']}`.\n{files}"
    )


def _review_summary(review: dict) -> str:
    """The other agent's read, marked as an opinion so the model does not take it for a test result."""
    who = f"{review['reviewer']} reviewed it (an opinion, not a check)"
    if review["verdict"] == "ok":
        return f"{who}: no concerns. {review['summary']}".strip()
    if review["verdict"] == "concerns":
        return f"{who}: CONCERNS.\n{review['summary']}"
    return f"{who}: no clear verdict.\n{review['summary']}".strip()


def _tests_summary(verification: dict | None) -> str:
    if verification is None:
        return ""
    command = f" (`{verification['command']}`)" if verification["command"] else ""
    state = verification["state"]
    if state == "passed":
        return f"north ran the tests{command} on them: passed."
    if state == "failed":
        return f"north ran the tests{command} on them: FAILED.\n{verification['detail']}"
    return f"The tests were not run{command}: {verification['detail']}."
