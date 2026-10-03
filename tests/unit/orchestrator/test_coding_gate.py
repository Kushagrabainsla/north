"""What a coding agent asks to do is ruled on by north's approval layer, like any other tool's action."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import orchestrator.coding_gate as coding_gate
from approval.models import ApprovalDecision
from approval.store import ApprovalStore
from coding_agents import Decision, GateSession, ToolRequest
from config.approval_mode import ApprovalMode
from orchestrator.coding_gate import ApprovalJudge, describe
from tests.conftest import approvals, deciding

WORKTREE = "/work/copy"
SESSION = GateSession(token="tok", run_id="run-1", task_id="t1", worktree=WORKTREE)


class Statuses:
    def __init__(self) -> None:
        self.calls: list[bool] = []

    async def waiting(self, run_id: str, waiting: bool) -> None:
        self.calls.append(waiting)


def _judge(mode: ApprovalMode, store: ApprovalStore, statuses: Statuses | None = None, **kw) -> ApprovalJudge:
    return ApprovalJudge(approvals(mode, store=store, **kw), statuses or Statuses())


async def _answer(store: ApprovalStore, decision: ApprovalDecision) -> object:
    for _ in range(200):
        if store.pending():
            card = store.pending()[0]
            store.resolve(card.id, decision)
            return card
        await asyncio.sleep(0.01)
    raise TimeoutError("no card surfaced")


BASH = ToolRequest("Bash", command="make test")
INSIDE = ToolRequest("Write", path=f"{WORKTREE}/src/a.py")
OUTSIDE = ToolRequest("Write", path="/etc/hosts")


class TestWhatBecomesACard:
    async def test_a_command_asks_and_the_card_names_the_task_and_the_command(self) -> None:
        store = ApprovalStore()
        judge = _judge(ApprovalMode.ASK, store)

        verdict, card = await asyncio.gather(
            judge.judge(SESSION, BASH, inside_worktree=False), _answer(store, ApprovalDecision.APPROVED)
        )

        assert verdict.decision is Decision.ALLOW
        assert (card.task_id, card.agent, card.blocking) == ("t1", "coding_agent", True)
        assert "make test" in card.message and "isolated copy" in card.message

    async def test_declining_denies_and_says_a_person_declined(self) -> None:
        store = ApprovalStore()
        judge = _judge(ApprovalMode.ASK, store)

        verdict, _ = await asyncio.gather(
            judge.judge(SESSION, BASH, inside_worktree=False), _answer(store, ApprovalDecision.REJECTED)
        )

        assert verdict.decision is Decision.DENY and "declined" in verdict.reason

    @pytest.mark.parametrize("mode", list(ApprovalMode))
    async def test_an_edit_inside_the_copy_never_asks_in_any_mode(self, mode) -> None:
        store = ApprovalStore()

        verdict = await _judge(mode, store).judge(SESSION, INSIDE, inside_worktree=True)

        assert verdict.decision is Decision.ALLOW and not store.pending()

    async def test_an_edit_outside_the_copy_asks_and_the_card_says_so(self) -> None:
        store = ApprovalStore()
        judge = _judge(ApprovalMode.ASK, store)

        verdict, card = await asyncio.gather(
            judge.judge(SESSION, OUTSIDE, inside_worktree=False), _answer(store, ApprovalDecision.REJECTED)
        )

        assert verdict.decision is Decision.DENY and "OUTSIDE the copy" in card.message

    async def test_yolo_says_yes_to_a_command_without_a_card(self) -> None:
        store = ApprovalStore()

        verdict = await _judge(ApprovalMode.YOLO, store).judge(SESSION, BASH, inside_worktree=False)

        assert verdict.decision is Decision.ALLOW and not store.pending()

    async def test_autonomous_leaves_it_to_the_memory_decider(self) -> None:
        store = ApprovalStore()
        judge = _judge(ApprovalMode.AUTONOMOUS, store, decider=deciding("approved", "Approve"))

        verdict = await judge.judge(SESSION, BASH, inside_worktree=False)

        assert verdict.decision is Decision.ALLOW and not store.pending()


class TestTheActionTheGateDescribes:
    def test_a_push_leaves_the_sandbox_and_an_ordinary_command_does_not(self) -> None:
        push = describe(SESSION, ToolRequest("Bash", command="git push origin main"), inside_worktree=False)
        build = describe(SESSION, BASH, inside_worktree=False)

        assert push.action.leaves_sandbox and not build.action.leaves_sandbox

    def test_an_edit_inside_is_isolated_and_one_outside_leaves_the_sandbox(self) -> None:
        inside = describe(SESSION, INSIDE, inside_worktree=True).action
        outside = describe(SESSION, OUTSIDE, inside_worktree=False).action

        assert (inside.isolated, inside.leaves_sandbox) == (True, False)
        assert (outside.isolated, outside.leaves_sandbox) == (False, True)
        assert outside.path == Path("/etc/hosts") and outside.workspace == WORKTREE

    def test_a_web_request_leaves_the_sandbox(self) -> None:
        action = describe(SESSION, ToolRequest("WebFetch", url="https://example.com"), inside_worktree=False).action

        assert action.leaves_sandbox and action.args == "https://example.com"

    def test_the_remembered_answer_for_a_command_is_the_command_itself(self) -> None:
        first = describe(SESSION, BASH, inside_worktree=False).action.describe()
        other_run = describe(GateSession("t2", "run-2", "t9", "/other"), BASH, inside_worktree=False).action.describe()

        assert first == other_run


class TestTheRunShowsItIsWaiting:
    async def test_a_run_waiting_on_a_card_says_so_and_goes_back_to_running(self, monkeypatch) -> None:
        monkeypatch.setattr(coding_gate, "_WAITING_AFTER_SECONDS", 0.05)
        store, statuses = ApprovalStore(), Statuses()
        judge = _judge(ApprovalMode.ASK, store, statuses)

        task = asyncio.create_task(judge.judge(SESSION, BASH, inside_worktree=False))
        await asyncio.sleep(0.2)
        assert statuses.calls == [True]
        await _answer(store, ApprovalDecision.APPROVED)
        await task

        assert statuses.calls == [True, False]

    async def test_a_run_that_is_answered_at_once_never_flickers_as_waiting(self, monkeypatch) -> None:
        monkeypatch.setattr(coding_gate, "_WAITING_AFTER_SECONDS", 0.05)
        statuses = Statuses()

        await _judge(ApprovalMode.YOLO, ApprovalStore(), statuses).judge(SESSION, BASH, inside_worktree=False)
        await asyncio.sleep(0.1)

        assert statuses.calls == []
