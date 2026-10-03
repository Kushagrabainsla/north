"""Offering an agent's change to the user and, when they agree, putting it in their working tree."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

from agents.workspace_lock import workspace_lock
from approval.approvals import Approvals, Request
from approval.policy import Action, ActionKind
from coding_agents import (
    Landing,
    LandingState,
    Review,
    ReviewVerdict,
    Verification,
    VerificationState,
    WorkChange,
)
from orchestrator.worktree import GitWorktreeManager, Worktree

AGENT = "coding_agent"


class LandingDesk:
    """The one place a coding run's change can reach the real working tree.

    It is offered through the approval layer, so the user's mode and memory decide. A change whose tests
    failed is not offered at all. Whatever happens, the agent's copy is removed afterwards; the branch is
    kept unless the change was applied.
    """

    def __init__(self, approvals: Approvals, lock: Callable[[str], asyncio.Lock] = workspace_lock) -> None:
        self._approvals = approvals
        self._lock = lock

    async def land(
        self, change: WorkChange, verification: Verification, task_id: str, review: Review | None = None
    ) -> Landing:
        tree = change.tree
        manager = GitWorktreeManager(tree.base)
        copy = Worktree(base=tree.base, path=tree.path, branch=tree.branch, base_sha=tree.base_sha)
        keep_branch = True
        try:
            if verification.state is VerificationState.FAILED:
                return Landing(LandingState.KEPT, "the tests failed, so it was not offered")
            decision = await self._approvals.decide(_request(change, verification, review), task_id=task_id)
            if not decision.allowed:
                if decision.status is None:  # refused outright, nobody was asked
                    return Landing(LandingState.KEPT, f"north's policy: {decision.reason}")
                return Landing(LandingState.DECLINED, "you kept it on the branch")
            if await manager.apply_back(copy, lock=self._lock(tree.base)):
                keep_branch = False
                return Landing(LandingState.APPLIED)
            return Landing(LandingState.CONFLICT, "the same lines changed in your working tree")
        finally:
            await manager.remove(copy, keep_branch=keep_branch)


def _request(change: WorkChange, verification: Verification, review: Review | None = None) -> Request:
    """The change as facts for the policy and as a card for a person."""
    tree = change.tree
    files = "\n".join(f"- {f.path} (+{f.insertions} -{f.deletions})" for f in change.files[:20])
    tests = _tests_line(verification)
    if review is not None:
        tests += "\n" + _review_lines(review)
    action = Action(
        agent=AGENT,
        kind=ActionKind.OTHER,
        summary=f"Apply {len(change.files)} file(s) from {tree.branch} to {tree.base}",
        operation="apply",
        # One answer per change: approving this branch must not approve the next.
        args=tree.branch,
        path=Path(tree.base),
        workspace=tree.base,
        details=f"{files}\n{tests}",
    )
    return Request(
        action,
        "Apply Coding Agent Changes - Approval Required",
        f"Apply the coding agent's changes to `{tree.base}` as uncommitted changes? "
        f"(+{change.insertions} -{change.deletions})\n\n{files}\n\n{tests}",
        declined="Changes left on their branch.",
    )


def _review_lines(review: Review) -> str:
    """The other agent's read, marked as advice: it is another model's opinion, not a check."""
    if review.verdict is ReviewVerdict.OK:
        return (
            f"Review by {review.reviewer} (another agent's opinion, not a check): no concerns. {review.summary}".strip()
        )
    head = "concerns" if review.verdict is ReviewVerdict.CONCERNS else "no clear verdict"
    return f"Review by {review.reviewer} (another agent's opinion, not a check): {head}.\n{review.summary}".strip()


def _tests_line(verification: Verification) -> str:
    command = f" (`{verification.command}`)" if verification.command else ""
    if verification.state is VerificationState.PASSED:
        return f"Tests: passed{command}, run by north on the agent's copy."
    return f"Tests: not verified{command} - {verification.detail}."
