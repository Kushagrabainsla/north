"""Putting an agent's change in the real working tree: asked first, applied cleanly, branch kept otherwise."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from approval.approvals import Approvals
from approval.models import ApprovalDecision
from approval.policy import ApprovalPolicy
from approval.store import ApprovalStore
from coding_agents import LandingState, Verification, VerificationState
from config.approval_mode import ApprovalMode
from orchestrator.coding_landing import LandingDesk
from orchestrator.coding_workspaces import GitWorkspaces
from tests.conftest import approvals

PASSED = Verification(VerificationState.PASSED, "pytest -q")


def _git(args: list[str], cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    directory = tmp_path / "repo"
    directory.mkdir()
    _git(["init", "-q"], directory)
    _git(["config", "user.name", "t"], directory)
    _git(["config", "user.email", "t@t"], directory)
    (directory / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    _git(["add", "-A"], directory)
    _git(["commit", "-qm", "init"], directory)
    return directory


async def _change(repo: Path, new_text: str = "def add(a, b):\n    return a + b\n\ndef sub(a, b):\n    return a - b\n"):
    """An agent's finished edit: a copy with a committed change."""
    workspaces = GitWorkspaces(repo.parent / "copies")
    tree = await workspaces.create(str(repo), "coding-land")
    Path(tree.path, "calc.py").write_text(new_text)
    return await workspaces.finish(tree)


async def _answer(store: ApprovalStore, decision: ApprovalDecision) -> object:
    for _ in range(200):
        if store.pending():
            card = store.pending()[0]
            store.resolve(card.id, decision)
            return card
        await asyncio.sleep(0.01)
    raise TimeoutError("no card surfaced")


def _branches(repo: Path) -> str:
    return _git(["branch", "--list"], repo)


async def _land(desk: LandingDesk, change, verification=PASSED, store=None, answer=None):
    if store is None:
        return await desk.land(change, verification, "t1"), None
    landing, card = await asyncio.gather(desk.land(change, verification, "t1"), _answer(store, answer))
    return landing, card


class TestWhenYouAgree:
    async def test_the_change_lands_uncommitted_and_the_copy_and_branch_are_gone(self, repo) -> None:
        change = await _change(repo)
        store = ApprovalStore()
        desk = LandingDesk(approvals(ApprovalMode.ASK, store=store))

        landing, card = await _land(desk, change, store=store, answer=ApprovalDecision.APPROVED)

        assert landing.state is LandingState.APPLIED
        assert "def sub" in (repo / "calc.py").read_text()
        assert " M calc.py" in _git(["status", "--porcelain"], repo), "uncommitted: your history is untouched"
        assert not Path(change.tree.path).exists() and change.tree.branch not in _branches(repo)
        assert card.task_id == "t1"

    async def test_the_card_shows_the_files_and_what_the_tests_said(self, repo) -> None:
        change = await _change(repo)
        store = ApprovalStore()
        desk = LandingDesk(approvals(ApprovalMode.ASK, store=store))

        _, card = await _land(desk, change, store=store, answer=ApprovalDecision.REJECTED)

        assert "calc.py (+3 -0)" in card.message and "passed (`pytest -q`)" in card.message
        assert "uncommitted" in card.message

    async def test_unverified_work_is_still_offered_with_the_reason_it_is_unverified(self, repo) -> None:
        change = await _change(repo)
        store = ApprovalStore()
        desk = LandingDesk(approvals(ApprovalMode.ASK, store=store))
        skipped = Verification(VerificationState.SKIPPED, detail="no test command was found for this project")

        landing, card = await _land(desk, change, skipped, store, ApprovalDecision.APPROVED)

        assert landing.state is LandingState.APPLIED
        assert "not verified" in card.message and "no test command was found" in card.message

    async def test_yolo_applies_without_a_card(self, repo) -> None:
        change = await _change(repo)

        landing, _ = await _land(LandingDesk(approvals(ApprovalMode.YOLO)), change)

        assert landing.state is LandingState.APPLIED and "def sub" in (repo / "calc.py").read_text()


class TestWhenItStaysOnTheBranch:
    async def test_declining_keeps_the_branch_and_leaves_your_tree_alone(self, repo) -> None:
        change = await _change(repo)
        store = ApprovalStore()
        desk = LandingDesk(approvals(ApprovalMode.ASK, store=store))

        landing, _ = await _land(desk, change, store=store, answer=ApprovalDecision.REJECTED)

        assert landing.state is LandingState.DECLINED
        assert "def sub" not in (repo / "calc.py").read_text()
        assert change.tree.branch in _branches(repo) and not Path(change.tree.path).exists()
        assert "def sub" in _git(["show", f"{change.tree.branch}:calc.py"], repo)

    async def test_a_change_whose_tests_failed_is_not_even_offered(self, repo) -> None:
        change = await _change(repo)
        store = ApprovalStore()
        failed = Verification(VerificationState.FAILED, "pytest -q", "1 failed")

        # A card never expires, so an offer that wrongly happened would wait forever: fail fast instead.
        desk = LandingDesk(approvals(ApprovalMode.ASK, store=store))
        landing = await asyncio.wait_for(desk.land(change, failed, "t1"), timeout=3)

        assert landing.state is LandingState.KEPT and "tests failed" in landing.reason
        assert not store.pending() and change.tree.branch in _branches(repo)

    async def test_the_same_lines_changing_meanwhile_is_a_conflict_and_nothing_is_overwritten(self, repo) -> None:
        change = await _change(repo, "def add(a, b):\n    return b + a  # agent\n")
        (repo / "calc.py").write_text("def add(a, b):\n    return a + b  # you\n")

        landing, _ = await _land(LandingDesk(approvals(ApprovalMode.YOLO)), change)

        assert landing.state is LandingState.CONFLICT
        assert (repo / "calc.py").read_text() == "def add(a, b):\n    return a + b  # you\n"
        assert change.tree.branch in _branches(repo)

    async def test_when_nobody_can_be_asked_it_stays_on_the_branch(self, repo) -> None:
        change = await _change(repo)
        nobody = Approvals(ApprovalPolicy(mode_provider=lambda: ApprovalMode.ASK), None)

        landing, _ = await _land(LandingDesk(nobody), change)

        assert landing.state is LandingState.KEPT and "north's policy" in landing.reason
        assert change.tree.branch in _branches(repo)

    async def test_a_change_to_a_different_branch_is_a_different_question(self, repo) -> None:
        first, second = await _change(repo), await _change(repo)
        from orchestrator.coding_landing import _request

        assert _request(first, PASSED).action.describe() != _request(second, PASSED).action.describe()


async def test_the_apply_waits_for_the_workspace_lock(repo) -> None:
    change = await _change(repo)
    lock = asyncio.Lock()
    desk = LandingDesk(approvals(ApprovalMode.YOLO), lock=lambda workspace: lock)

    await lock.acquire()
    landing = asyncio.create_task(desk.land(change, PASSED, "t1"))
    await asyncio.sleep(0.2)
    assert not landing.done(), "another run is changing the same tree"
    lock.release()

    assert (await landing).state is LandingState.APPLIED
