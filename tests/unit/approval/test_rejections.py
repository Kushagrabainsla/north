"""A "no" holds for the rest of the task, reads included (issue #76, finding M1).

After a rejected `cd /home/user/north && ls && cat README.md`, the agent read the
same folder with the read tools of the time, which never asked, and answered as if
nobody had objected. Since issue #78 the shell is the only reader, and a command
that only reads may run without a card: the "no" must still hold for it.
"""

from __future__ import annotations

from pathlib import Path

from approval.rejections import RejectionLog
from tests.conftest import approvals, approving_store, rejecting_store
from tools.models import ToolInput
from tools.specialized.bash import BashTool
from tools.universal.write_file import WriteFileTool


def _bound(tool, layer):
    tool.approvals = layer
    return tool


def test_a_rejection_covers_the_path_and_everything_under_it(tmp_path: Path) -> None:
    log = RejectionLog(tmp_path / "approvals.db")
    log.record("t1", [Path("/home/user/north")], "cd /home/user/north && ls")

    assert log.covering("t1", Path("/home/user/north/README.md")) == ("/home/user/north", "cd /home/user/north && ls")
    assert log.covering("t1", Path("/home/user/north")) is not None
    assert log.covering("t1", Path("/home/user/northwind/README.md")) is None
    assert log.covering("t2", Path("/home/user/north/README.md")) is None


def test_a_rejection_outlives_a_restart(tmp_path: Path) -> None:
    RejectionLog(tmp_path / "approvals.db").record("t1", [Path("/srv/repo")], "ls /srv/repo")

    assert RejectionLog(tmp_path / "approvals.db").covering("t1", Path("/srv/repo/a.py")) is not None


async def test_after_a_rejected_command_the_same_folder_cannot_be_read_another_way(tmp_path: Path) -> None:
    repo = tmp_path / "north"
    repo.mkdir()
    (repo / "README.md").write_text("plans", encoding="utf-8")
    rejections = RejectionLog(tmp_path / "approvals.db")
    rejected = _bound(BashTool(), approvals(store=rejecting_store(), rejections=rejections))
    command = await rejected.execute(
        ToolInput(params={"command": f"cd {repo} && ls && cat README.md", "task_id": "t1"})
    )
    # Whoever would answer next would say yes: the earlier "no" must decide first.
    store = approving_store()
    bash = _bound(BashTool(os_sandbox=True), approvals(store=store, rejections=rejections))

    read_again = await bash.execute(ToolInput(params={"command": f"cat {repo}/README.md", "task_id": "t1"}))
    list_again = await bash.execute(ToolInput(params={"command": f"ls {repo}", "task_id": "t1"}))

    assert not command.success
    assert read_again.failure_kind == "refused" and "you said no" in read_again.error
    assert list_again.failure_kind == "refused" and "you said no" in list_again.error
    store.wait_for_decision.assert_not_awaited()


async def test_another_task_or_another_folder_is_not_covered(tmp_path: Path) -> None:
    repo, other = tmp_path / "north", tmp_path / "notes"
    for folder in (repo, other):
        folder.mkdir()
        (folder / "a.md").write_text("a", encoding="utf-8")
    rejections = RejectionLog(tmp_path / "approvals.db")
    await _bound(BashTool(), approvals(store=rejecting_store(), rejections=rejections)).execute(
        ToolInput(params={"command": f"ls {repo}", "task_id": "t1"})
    )
    bash = _bound(BashTool(), approvals(store=approving_store(), rejections=rejections))

    assert (await bash.execute(ToolInput(params={"command": f"cat {repo}/a.md", "task_id": "t2"}))).success
    assert (await bash.execute(ToolInput(params={"command": f"cat {other}/a.md", "task_id": "t1"}))).success


async def test_a_change_to_a_rejected_path_is_refused_without_asking_again(tmp_path: Path) -> None:
    repo = tmp_path / "north"
    repo.mkdir()
    rejections = RejectionLog(tmp_path / "approvals.db")
    await _bound(BashTool(), approvals(store=rejecting_store(), rejections=rejections)).execute(
        ToolInput(params={"command": f"ls {repo}", "task_id": "t1"})
    )
    store = approving_store()
    write = _bound(WriteFileTool(), approvals(store=store, rejections=rejections))

    output = await write.execute(ToolInput(params={"path": str(repo / "notes.md"), "content": "x", "task_id": "t1"}))

    assert output.failure_kind == "refused" and "you said no" in output.error
    store.wait_for_decision.assert_not_awaited()
    assert not (repo / "notes.md").exists()
