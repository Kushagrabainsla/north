"""A command that writes only to its task's own folders, with no network, needs no approval.

On 2026-11-18 a LinkedIn outreach task sent about 19 shell commands to the approval
model, 4 to 39 s each. They saved notes for the task, wrote small scripts to temp and
ran them, or cropped images in the task folder. A write_file to the same folders needed
no approval. Now such a command runs first in a box that lets it write only to its
task's folder, its task's notes folder and temp, with no network and no shared caches.
If it finishes cleanly there, it needs no approval, and it does not run again.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from approval.policy import Action, ActionKind, ApprovalMode, ApprovalPolicy
from tests.conftest import bind_approvals
from tools.models import ToolInput
from tools.specialized import _seatbelt
from tools.specialized.bash import BashTool
from utils.handoff import task_of_handoff_dir

needs_seatbelt = pytest.mark.skipif(not _seatbelt.available(), reason="needs macOS Seatbelt")


@pytest.fixture
def home(tmp_path, move_north_home):
    home = move_north_home(tmp_path / "north")
    for folder in ("tasks/t1", "tasks/t2", "notes/t1", "notes/t2"):
        (home / folder).mkdir(parents=True)
    return home


def _store():
    """A card store that fails the test if anything is asked."""
    store = MagicMock()
    store.wait_for_decision = AsyncMock(side_effect=AssertionError("a card was shown"))
    return store


async def _run(home, command: str, task: str = "t1", mode: ApprovalMode = ApprovalMode.ASK):
    tool = bind_approvals(BashTool(os_sandbox=True), mode, store=_store())
    return await tool.execute(
        ToolInput(params={"command": command, "task_id": task}, granted_workspace=str(home / "tasks" / task))
    )


def test_the_contained_box_has_no_network_and_no_shared_caches() -> None:
    contained = _seatbelt.profile("/w", writable=True, contained=True, proxy_port=9999)
    approved = _seatbelt.profile("/w", writable=True, proxy_port=9999)

    assert "(deny network*)" in contained and "localhost:*" not in contained
    assert "Library/Caches" not in contained and "Library/Caches" in approved
    assert '(subpath "/w")' in contained


def test_only_a_task_s_own_folder_names_a_task(home) -> None:
    assert task_of_handoff_dir(str(home / "tasks" / "t1")) == "t1"
    assert task_of_handoff_dir(str(home / "tasks" / "t1" / "sub")) is None
    assert task_of_handoff_dir(str(home / "tasks")) is None
    assert task_of_handoff_dir(str(home / "notes" / "t1")) is None


@needs_seatbelt
async def test_saving_to_the_task_s_own_folders_asks_nobody_and_runs_once(home) -> None:
    out = await _run(
        home,
        f"echo one >> {home / 'notes/t1/log.md'}; echo shot > crop.txt; "
        f"printf 'print(2)' > /tmp/north-contained-test.py; echo done",
    )

    assert out.success, out.error
    assert (home / "notes/t1/log.md").read_text() == "one\n"  # appended once, not twice
    assert (home / "tasks/t1/crop.txt").read_text() == "shot\n"


@needs_seatbelt
async def test_a_write_to_another_task_s_folder_still_asks(home) -> None:
    with pytest.raises(AssertionError, match="a card was shown"):
        await _run(home, f"echo x >> {home / 'notes/t2/log.md'}")

    assert not (home / "notes/t2/log.md").exists()


@needs_seatbelt
async def test_a_command_that_needs_the_network_still_asks(home) -> None:
    with pytest.raises(AssertionError, match="a card was shown"):
        await _run(home, "echo x > a.txt; /usr/bin/curl -sS --max-time 5 https://example.com > page.html")


@needs_seatbelt
async def test_a_command_in_a_folder_of_yours_still_asks(home, tmp_path) -> None:
    yours = tmp_path / "project"
    yours.mkdir()
    tool = bind_approvals(BashTool(os_sandbox=True), ApprovalMode.ASK, store=_store())

    with pytest.raises(AssertionError, match="a card was shown"):
        await tool.execute(ToolInput(params={"command": "echo x > a.txt"}, granted_workspace=str(yours)))

    assert not (yours / "a.txt").exists()


@needs_seatbelt
async def test_a_card_after_the_box_says_it_already_ran_once(home) -> None:
    tool = BashTool(os_sandbox=True)
    request = await tool.describe(
        ToolInput(params={"command": "echo x > a.txt; exit 3"}, granted_workspace=str(home / "tasks" / "t1"))
    )

    assert request is not None and not request.action.ran_contained
    assert "already ran it once" in request.message


async def test_the_policy_allows_a_contained_run_in_every_mode_but_not_one_that_leaves() -> None:
    contained = Action(agent="bash", kind=ActionKind.SHELL_COMMAND, summary="x", command="x", ran_contained=True)
    leaving = Action(
        agent="bash", kind=ActionKind.SHELL_COMMAND, summary="x", command="x", ran_contained=True, leaves_sandbox=True
    )
    for mode in (ApprovalMode.ASK, ApprovalMode.SAFE, ApprovalMode.AUTONOMOUS):
        policy = ApprovalPolicy(mode_provider=lambda mode=mode: mode)
        assert (await policy.rule(contained)).allowed
        assert not (await policy.rule(leaving)).allowed
