"""write_file goes through the same approval gate as patch_file.

It used to write without asking in every mode, and AUTO judged "inside the
workspace" by the model's own `workspace` argument. Only the server's grant
(`ToolInput.granted_workspace`) counts now.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from config.approval_mode import ApprovalMode
from tests.conftest import bind_approvals
from tools.models import ToolInput
from tools.universal.write_file import WriteFileTool


def _silent_store() -> MagicMock:
    """A store where a surfaced card is never answered."""
    store = MagicMock()
    store.wait_for_decision = AsyncMock(return_value=None)
    return store


def _tool(mode: ApprovalMode, store: MagicMock) -> WriteFileTool:
    return bind_approvals(WriteFileTool(), mode, store=store)


@pytest.mark.asyncio
async def test_interactive_asks_before_writing(tmp_path: Path) -> None:
    store = _silent_store()
    target = tmp_path / "notes.md"
    target.write_text("old line\n")

    out = await _tool(ApprovalMode.ASK, store).execute(
        ToolInput(params={"path": str(target), "content": "new line\n"}, granted_workspace=str(tmp_path))
    )

    assert not out.success and out.failure_kind == "refused"
    assert target.read_text() == "old line\n"
    card = store.add.call_args.args[0]
    assert "-old line" in card.message and "+new line" in card.message  # the card shows what it replaces


@pytest.mark.asyncio
async def test_auto_writes_inside_the_granted_folder_without_a_card(tmp_path: Path) -> None:
    store = _silent_store()
    target = tmp_path / "src" / "m.py"

    out = await _tool(ApprovalMode.SAFE, store).execute(
        ToolInput(params={"path": str(target), "content": "x = 1\n"}, granted_workspace=str(tmp_path))
    )

    assert out.success, out.error
    assert target.read_text() == "x = 1\n"
    store.wait_for_decision.assert_not_awaited()


@pytest.mark.asyncio
async def test_auto_ignores_a_workspace_the_model_chose(tmp_path: Path) -> None:
    store = _silent_store()
    target = tmp_path / "elsewhere.md"

    out = await _tool(ApprovalMode.SAFE, store).execute(
        ToolInput(
            params={"path": str(target), "content": "x\n", "workspace": str(tmp_path)},
            granted_workspace=str(tmp_path / "task"),
        )
    )

    assert not out.success
    assert not target.exists()
    store.wait_for_decision.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_write_outside_the_task_folder_asks_and_then_lands(tmp_path: Path) -> None:
    """ "Save this to my Desktop" used to fail outright: the path was outside the folder, so no card was even made."""
    from tests.conftest import approving_store

    task, desktop = tmp_path / "task", tmp_path / "Desktop"
    target = desktop / "summary.md"
    params = {"path": str(target), "content": "hi\n", "workspace": str(task)}

    out = await _tool(ApprovalMode.SAFE, approving_store()).execute(
        ToolInput(params=params, granted_workspace=str(task))
    )

    assert out.success, out.error
    assert target.read_text() == "hi\n"


@pytest.mark.asyncio
async def test_a_credential_path_is_refused_before_any_card(tmp_path: Path, monkeypatch) -> None:
    from tools import _path

    monkeypatch.setenv("HOME", str(tmp_path))
    _path._resolved_blocked_prefixes.cache_clear()
    store = _silent_store()

    out = await _tool(ApprovalMode.ASK, store).execute(
        ToolInput(params={"path": str(tmp_path / ".ssh" / "authorized_keys"), "content": "k"}, granted_workspace="/x")
    )

    assert not out.success and "blocked" in out.error
    store.add.assert_not_called()


@pytest.mark.asyncio
async def test_a_file_outside_the_task_folder_can_be_read(tmp_path: Path) -> None:
    from tools.universal.read_file import ReadFileTool

    notes = tmp_path / "Desktop" / "notes.txt"
    notes.parent.mkdir()
    notes.write_text("remember the milk")

    out = await ReadFileTool().execute(
        ToolInput(
            params={"path": str(notes), "workspace": str(tmp_path / "task")}, granted_workspace=str(tmp_path / "task")
        )
    )

    assert out.success, out.error
    assert "remember the milk" in str(out.data)


@pytest.mark.asyncio
async def test_north_notes_are_written_without_a_card_in_interactive(tmp_path: Path) -> None:
    store = _silent_store()
    target = tmp_path / "home" / "notes" / "today.md"

    with patch.dict("os.environ", {"NORTH_HOME": str(tmp_path / "home")}):
        out = await _tool(ApprovalMode.ASK, store).execute(ToolInput(params={"path": str(target), "content": "done\n"}))

    assert out.success, out.error
    assert target.read_text() == "done\n"
    store.wait_for_decision.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_unbound_instance_refuses_rather_than_writes(tmp_path: Path) -> None:
    """With no approval layer there is nobody to ask - a missing gate is not an open one."""
    target = tmp_path / "a.txt"

    out = await WriteFileTool().execute(ToolInput(params={"path": str(target), "content": "a"}))

    assert out.failure_kind == "refused" and "fail closed" in out.error
    assert not target.exists()
