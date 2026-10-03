"""Rules on what a coding agent asks to do, through north's one approval layer.

The gate (`coding_agents/gate.py`) settles what it can by itself; what is left becomes an `Action` here
and goes to `Approvals.decide`, so a coding agent is held to the same policy, memory and modes as any
other tool. Nothing in this file decides - it describes (CODING_STYLE 7.3).
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Protocol

from approval.approvals import Approvals, Request
from approval.policy import Action, ActionKind
from coding_agents import Decision, GateSession, ToolRequest, Verdict
from coding_agents.gate import FILE_TOOLS

AGENT = "coding_agent"
# A run that is answered at once should not flicker on the dashboard as waiting.
_WAITING_AFTER_SECONDS = 0.5
_PUSH = re.compile(r"\bgit\s+push\b")


class RunStatuses(Protocol):
    async def waiting(self, run_id: str, waiting: bool) -> None: ...


class ApprovalJudge:
    """The approval layer, as the gate's `Judge`."""

    def __init__(self, approvals: Approvals, statuses: RunStatuses) -> None:
        self._approvals = approvals
        self._statuses = statuses

    async def judge(self, session: GateSession, request: ToolRequest, *, inside_worktree: bool) -> Verdict:
        asked = describe(session, request, inside_worktree=inside_worktree)
        shown = {"waiting": False}

        async def show_waiting_soon() -> None:
            await asyncio.sleep(_WAITING_AFTER_SECONDS)
            shown["waiting"] = True
            await self._statuses.waiting(session.run_id, True)

        marker = asyncio.create_task(show_waiting_soon())
        try:
            decision = await self._approvals.decide(asked, task_id=session.task_id)
        finally:
            marker.cancel()
            await asyncio.gather(marker, return_exceptions=True)
            if shown["waiting"]:
                await self._statuses.waiting(session.run_id, False)
        if decision.allowed:
            return Verdict(Decision.ALLOW, decision.reason)
        if decision.status is None:  # refused outright, nobody was asked
            return Verdict(Decision.DENY, f"Blocked by north's policy: {decision.reason}")
        return Verdict(Decision.DENY, "The user declined this action.")


def describe(session: GateSession, request: ToolRequest, *, inside_worktree: bool) -> Request:
    """What the agent asked to do, as facts for the policy and a card for a person."""
    context = f"A coding agent working in an isolated copy of your repository at `{session.worktree}`."
    if request.tool == "Bash":
        action = Action(
            agent=AGENT,
            kind=ActionKind.SHELL_COMMAND,
            summary=request.command,
            command=request.command,
            workspace=session.worktree,
            leaves_sandbox=bool(_PUSH.search(request.command)),
        )
        return Request(action, "Coding Agent Command - Approval Required", f"{context}\n\n```\n{request.command}\n```")
    if request.tool in FILE_TOOLS:
        path = Path(request.path)
        action = Action(
            agent=AGENT,
            kind=ActionKind.FILE_EDIT,
            summary=f"{request.tool} {path}",
            path=path,
            workspace=session.worktree,
            isolated=inside_worktree,
            leaves_sandbox=not inside_worktree,
        )
        where = "inside the copy" if inside_worktree else "OUTSIDE the copy"
        return Request(
            action, "Coding Agent File Change - Approval Required", f"{context}\n\n{request.tool} `{path}` ({where})"
        )
    target = request.url or request.tool
    action = Action(
        agent=AGENT,
        kind=ActionKind.MCP if request.tool.startswith("mcp__") else ActionKind.OTHER,
        summary=f"{request.tool} {request.url}".strip(),
        operation=request.tool,
        args=request.url,
        workspace=session.worktree,
        leaves_sandbox=True,
    )
    return Request(action, "Coding Agent Request - Approval Required", f"{context}\n\n{request.tool}: {target}")
