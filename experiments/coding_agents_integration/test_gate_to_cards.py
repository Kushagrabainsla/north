"""Question: does an agent's request reach north's approval layer as a real, correctly-owned card?

Every case drives production classes: `Approvals`, `ApprovalPolicy`, `UserInteraction`, `ApprovalStore`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from approval.approvals import Approvals, Request
from approval.interaction import UserInteraction
from approval.models import ApprovalDecision
from approval.policy import Action, ActionKind, Answer, ApprovalPolicy
from approval.store import ApprovalStore
from approval.unattended import UnattendedPolicy
from config.approval_mode import ApprovalMode

from .prototypes import record, until

WORKTREE = "/work/repo-wt"

REQUESTS: dict[str, Action] = {
    "read_in_worktree": Action(
        agent="coding:claude", kind=ActionKind.OTHER, summary="Read a.py", mutating=False, read_only=True
    ),
    "edit_in_worktree": Action(
        agent="coding:claude",
        kind=ActionKind.FILE_EDIT,
        summary="Edit a.py",
        path=Path(WORKTREE) / "a.py",
        workspace=WORKTREE,
    ),
    "shell_read_only": Action(
        agent="coding:claude",
        kind=ActionKind.SHELL_COMMAND,
        summary="git status",
        command="git status",
        mutating=False,
        read_only=True,
    ),
    "shell_write": Action(
        agent="coding:claude", kind=ActionKind.SHELL_COMMAND, summary="make build", command="make build"
    ),
    "git_push_as_shell": Action(
        agent="coding:claude",
        kind=ActionKind.SHELL_COMMAND,
        summary="git push origin main",
        command="git push origin main",
    ),
    "git_push_as_git": Action(agent="coding:claude", kind=ActionKind.GIT, summary="push origin main", operation="push"),
    "fetch_new_host": Action(
        agent="coding:claude", kind=ActionKind.OTHER, summary="WebFetch https://example.com", mutating=True
    ),
}


class _Decider:
    """A memory decider stand-in: approves everything, or abstains (returns None)."""

    def __init__(self, approves: bool) -> None:
        self._approves = approves

    async def rule(self, action: Action) -> Answer | None:
        if not self._approves:
            return None
        return Answer(ApprovalDecision.APPROVED, "Approve", "stub decider", "memory_decider")

    async def answer(self, card):  # noqa: ANN001
        return None


async def _verdict(mode: ApprovalMode, action: Action, decider: _Decider | None = None) -> tuple[str, str]:
    policy = ApprovalPolicy(mode_provider=lambda: mode, unattended=UnattendedPolicy(), decider=decider)
    ruling = await policy.rule(action)
    return ruling.verdict.value, ruling.rule


async def test_a_gate_request_becomes_a_blocking_card_owned_by_the_task() -> None:
    """The card must carry the task id and block it: that is what frees the concurrency slot."""
    store = ApprovalStore()
    approvals = Approvals(ApprovalPolicy(mode_provider=lambda: ApprovalMode.ASK), UserInteraction(store))
    action = REQUESTS["shell_write"]

    waiting = asyncio.create_task(approvals.decide(Request(action, "Shell", "make build"), task_id="t-run"))
    card = (await until(store.pending))[0]
    held_by_card = store.tasks_waiting_on_you()
    store.resolve(card.id, ApprovalDecision.APPROVED)
    decision = await waiting

    assert (card.task_id, card.blocking) == ("t-run", True)
    assert held_by_card == {"t-run"}
    assert decision.allowed
    record("G1_card", {"task_id": card.task_id, "blocking": card.blocking, "agent": card.agent, "title": card.title})


async def test_what_each_mode_does_with_each_request_a_coding_agent_makes() -> None:
    table: dict[str, dict[str, str]] = {}
    for mode, decider, label in [
        (ApprovalMode.ASK, None, "ask"),
        (ApprovalMode.SAFE, None, "safe"),
        (ApprovalMode.AUTONOMOUS, _Decider(approves=False), "autonomous, decider abstains"),
        (ApprovalMode.AUTONOMOUS, _Decider(approves=True), "autonomous, decider approves"),
        (ApprovalMode.YOLO, None, "yolo"),
    ]:
        table[label] = {}
        for name, action in REQUESTS.items():
            verdict, rule = await _verdict(mode, action, decider)
            table[label][name] = f"{verdict} ({rule})"
    record("G2_policy_table", table)

    # Reads never ask, in any mode.
    for label in table:
        assert table[label]["read_in_worktree"].startswith("allow")
        assert table[label]["shell_read_only"].startswith("allow")
    # Anything that changes something asks in ask mode.
    for name in ("edit_in_worktree", "shell_write", "git_push_as_shell", "git_push_as_git", "fetch_new_host"):
        assert table["ask"][name].startswith("ask")
    # Autonomous with an abstaining decider waits for you on everything that leaves the worktree - the
    # abstain rule rides on this. An edit inside the task workspace is already allowed by a fixed rule.
    for name in ("shell_write", "git_push_as_shell", "git_push_as_git", "fetch_new_host"):
        assert table["autonomous, decider abstains"][name].startswith("ask"), name
    assert table["autonomous, decider abstains"]["edit_in_worktree"] == "allow (edit inside the task workspace)"


async def test_autonomous_has_no_hard_floor_today_so_the_memory_rule_must_live_in_the_decider() -> None:
    """Pins current behaviour: an approving decider lets a push through. The abstain-without-a-fact
    rule is therefore a decider change (prompt + a 'leaves the sandbox' fact on the Action), not a policy floor."""
    verdict, _ = await _verdict(ApprovalMode.AUTONOMOUS, REQUESTS["git_push_as_git"], _Decider(approves=True))
    assert verdict == "allow"
