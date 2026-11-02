"""An approval card shows what approving changes, and north reports the change that was made.

In the issue #76 test (M13) Claude Code was asked to fix "helo" to "hello". The
apply card read only "(+2 -2) - README.md (+1 -1) - app/greet.py (+1 -1)", and
north and Claude Code both reported `"Hello, {name}!"` while the branch, and so
the applied file, held `"Hello,{name}!"`. Elsewhere (A18) a diff against a file
with no final newline ran two lines together ("-meeting moved to friday+ALL
MEETINGS CANCELLED"), and nine remembered writes in one folder all had the label
"write_file file_edit path=/tmp/claude-0/-home-user-north/17e1ae8d-673e-5f46-9673".
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from approval.approval_memory import _DISPLAY_SIGNATURE_CHARS, _display_signature
from approval.models import ApprovalDecision
from approval.store import ApprovalStore
from coding_agents import RunOutcome, Verification, VerificationState
from coding_agents.runner import RunReport
from config.approval_mode import ApprovalMode
from orchestrator.coding_landing import LandingDesk
from orchestrator.coding_workspaces import GitWorkspaces
from tests.conftest import approvals
from tools._diff import unified_diff
from tools.specialized.coding_agent import _edit_summary, _output

_GREET = 'def greet(name):\n    return f"Helo, {name}!"\n'
_APPLIED = 'def greet(name):\n    return f"Hello,{name}!"\n'


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, check=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    directory = tmp_path / "repo"
    directory.mkdir()
    _git(["init", "-q"], directory)
    _git(["config", "user.name", "t"], directory)
    _git(["config", "user.email", "t@t"], directory)
    (directory / "greet.py").write_text(_GREET)
    _git(["add", "-A"], directory)
    _git(["commit", "-qm", "init"], directory)
    return directory


async def _edited(repo: Path):
    workspaces = GitWorkspaces(repo.parent / "copies")
    tree = await workspaces.create(str(repo), "coding-greet")
    Path(tree.path, "greet.py").write_text(_APPLIED)
    return await workspaces.finish(tree)


async def test_the_apply_card_shows_the_diff_itself(repo: Path) -> None:
    change = await _edited(repo)
    store = ApprovalStore()
    desk = LandingDesk(approvals(ApprovalMode.ASK, store=store))

    async def reject() -> object:
        while not store.pending():
            await asyncio.sleep(0.01)
        card = store.pending()[0]
        store.resolve(card.id, ApprovalDecision.REJECTED)
        return card

    _, card = await asyncio.gather(desk.land(change, Verification(VerificationState.SKIPPED), "t1"), reject())

    assert "```diff" in card.message
    assert '+    return f"Hello,{name}!"' in card.message


async def test_north_is_handed_the_diff_to_report_from(repo: Path) -> None:
    change = await _edited(repo)
    report = RunReport("r1", "claude", RunOutcome(True, 'Changed it to "Hello, {name}!"', "s1"), change=change)

    data = _output(report).data

    assert '+    return f"Hello,{name}!"' in data["change"]["diff"]
    assert "change.diff" in _edit_summary(data) and "not from the agent's own account" in _edit_summary(data)


def test_a_diff_keeps_a_line_with_no_newline_apart_from_the_next() -> None:
    diff = unified_diff(Path("notes.txt"), "meeting moved to friday", "ALL MEETINGS CANCELLED")

    assert "-meeting moved to friday+" not in diff
    assert "-meeting moved to friday\n\\ No newline at end of file\n+ALL MEETINGS CANCELLED" in diff


def test_remembered_decisions_in_one_folder_have_labels_that_tell_them_apart() -> None:
    folder = "/tmp/claude-0/-home-user-north/17e1ae8d-673e-5f46-9673-8d46d9b2bd7f/scratchpad/harness/ws/approvals"
    labels = {_display_signature(f"write_file file_edit path={folder}/{name}") for name in ("a.txt", "b.txt")}

    assert len(labels) == 2
    assert all(len(label) <= _DISPLAY_SIGNATURE_CHARS and label.startswith("write_file") for label in labels)
