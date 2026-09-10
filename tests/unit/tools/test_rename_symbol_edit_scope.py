"""Edit-scope enforcement for the *multi-file* mutation path (RenameSymbolTool).

Unlike WriteFileTool/PatchFileTool - which resolve one model-named path and can
guard it directly - a semantic rename drives a language server whose
``WorkspaceEdit`` may touch files the coder never named. Enforcement therefore
has to happen where the edit set is known: in ``_apply_workspace_edit``, which
authorizes EVERY file the rename would write *before* writing any of them and
aborts atomically on the first refusal.

These tests exercise that guarantee three ways:

* ``_apply_workspace_edit`` directly, with a fabricated multi-file WorkspaceEdit,
  so the authorization/atomicity logic is verified without a language server.
* ``RenameSymbolTool`` end-to-end, mocking only the LSP *connection* so the real
  rename + apply path runs and an unauthorized second file blocks the whole write.
* The tool's no-scope path, proving a ``None`` scope preserves prior behavior.

A companion test covers the Orchestrator direct single-tool dispatch stamping the
server-owned scope onto ``ToolInput.edit_scope`` (never ``params``).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

import context.lsp_client as lsp
from architecture.scopes import TaskEditScope, build_guard
from context.lsp_client import RenameScopeRefused, _apply_workspace_edit
from tools.models import ToolInput
from tools.specialized.rename_symbol import RenameSymbolTool


def _guard(workspace: Path, scope: TaskEditScope):
    return build_guard(scope, workspace)


def _workspace_edit(*paths: Path) -> dict:
    """A minimal LSP WorkspaceEdit that replaces byte 0 of each path with 'Y'."""
    zero_range = {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 1}}
    return {"changes": {"file://" + str(p): [{"range": zero_range, "newText": "Y"}] for p in paths}}


# --------------------------------------------------------------------------- #
# _apply_workspace_edit: all-or-nothing authorization                         #
# --------------------------------------------------------------------------- #


def test_apply_denies_when_any_file_outside_scope_and_writes_nothing(tmp_path: Path) -> None:
    # Two files in different modules; the scope permits only one. A partial write
    # would corrupt the rename, so the whole edit must abort with nothing written.
    (tmp_path / "config").mkdir()
    (tmp_path / "agents").mkdir()
    permitted = tmp_path / "config" / "settings.py"
    forbidden = tmp_path / "agents" / "models.py"
    permitted.write_text("Xreach = 1\n", encoding="utf-8")
    forbidden.write_text("Xreach = 1\n", encoding="utf-8")

    guard = _guard(tmp_path, TaskEditScope(modules=("platform.config",)))
    edit = _workspace_edit(permitted, forbidden)

    with pytest.raises(RenameScopeRefused) as exc:
        _apply_workspace_edit(tmp_path, edit, guard.authorize)

    assert "agents/models.py" in str(exc.value) or "models.py" in str(exc.value)
    # Not a single byte written - the permitted file is untouched too.
    assert permitted.read_text() == "Xreach = 1\n"
    assert forbidden.read_text() == "Xreach = 1\n"


def test_apply_writes_all_when_every_file_authorized(tmp_path: Path) -> None:
    (tmp_path / "config").mkdir()
    a = tmp_path / "config" / "a.py"
    b = tmp_path / "config" / "b.py"
    a.write_text("Xreach = 1\n", encoding="utf-8")
    b.write_text("Xreach = 1\n", encoding="utf-8")

    guard = _guard(tmp_path, TaskEditScope(modules=("platform.config",)))
    files, edits, changed = _apply_workspace_edit(tmp_path, _workspace_edit(a, b), guard.authorize)

    assert files == 2
    assert edits == 2
    assert sorted(changed) == ["config/a.py", "config/b.py"]
    assert a.read_text().startswith("Y")
    assert b.read_text().startswith("Y")


def test_apply_none_authorizer_preserves_unrestricted_behavior(tmp_path: Path) -> None:
    # A None authorizer (every current public caller) writes every in-workspace file.
    (tmp_path / "agents").mkdir()
    f = tmp_path / "agents" / "models.py"
    f.write_text("Xreach = 1\n", encoding="utf-8")

    files, _edits, changed = _apply_workspace_edit(tmp_path, _workspace_edit(f), None)

    assert files == 1
    assert changed == ["agents/models.py"]
    assert f.read_text().startswith("Y")


# --------------------------------------------------------------------------- #
# RenameSymbolTool end-to-end (LSP connection mocked)                          #
# --------------------------------------------------------------------------- #


def _install_fake_lsp(monkeypatch: pytest.MonkeyPatch, changed_paths: list[Path]) -> None:
    """Make the real rename path run against a fake language server.

    ``server_command_for`` is forced to a dummy command (so the tool's
    availability check passes without pyright), ``_name_position`` returns a fixed
    position (so we need not craft parseable source), ``_open`` is a no-op, and the
    connection's ``request`` yields a WorkspaceEdit touching *changed_paths*. The
    real ``_apply_workspace_edit`` - and thus the scope authorization - then runs.
    """
    monkeypatch.setattr(lsp, "server_command_for", lambda _suffix: ["fake-langserver"])
    # RenameSymbolTool imported server_command_for into its own namespace, so the
    # tool's availability check reads that binding - patch it there too.
    import tools.specialized.rename_symbol as rename_mod

    monkeypatch.setattr(rename_mod, "server_command_for", lambda _suffix: ["fake-langserver"])
    monkeypatch.setattr(lsp, "_name_position", lambda _text, _symbol: (0, 0))
    monkeypatch.setattr(lsp, "_open", lambda *_args, **_kwargs: None)

    class _FakeConn:
        def __init__(self, _cmd):
            pass

        def request(self, _method, _params, timeout=None):
            return _workspace_edit(*changed_paths)

        def close(self):
            pass

    monkeypatch.setattr(lsp, "_Connection", _FakeConn)


@pytest.mark.asyncio
async def test_rename_refuses_multi_file_write_outside_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The rename's WorkspaceEdit touches a permitted file AND a forbidden one; the
    # tool must refuse and leave BOTH untouched.
    (tmp_path / "config").mkdir()
    (tmp_path / "agents").mkdir()
    defining = tmp_path / "config" / "settings.py"
    forbidden = tmp_path / "agents" / "models.py"
    defining.write_text("old_name = 1\n", encoding="utf-8")
    forbidden.write_text("old_name = 1\n", encoding="utf-8")

    _install_fake_lsp(monkeypatch, [defining, forbidden])
    guard = _guard(tmp_path, TaskEditScope(modules=("platform.config",)))

    out = await RenameSymbolTool().run(
        ToolInput(
            params={
                "path": "config/settings.py",
                "symbol": "old_name",
                "new_name": "new_name",
                "workspace": str(tmp_path),
            },
            edit_scope=guard,
        )
    )

    assert not out.success
    assert out.failure_kind == "refused"
    # Atomic abort: neither the forbidden nor the permitted file was written.
    assert forbidden.read_text() == "old_name = 1\n"
    assert defining.read_text() == "old_name = 1\n"


@pytest.mark.asyncio
async def test_rename_allows_multi_file_write_within_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "config").mkdir()
    defining = tmp_path / "config" / "settings.py"
    other = tmp_path / "config" / "helpers.py"
    defining.write_text("old_name = 1\n", encoding="utf-8")
    other.write_text("old_name = 1\n", encoding="utf-8")

    _install_fake_lsp(monkeypatch, [defining, other])
    guard = _guard(tmp_path, TaskEditScope(modules=("platform.config",)))

    out = await RenameSymbolTool().run(
        ToolInput(
            params={
                "path": "config/settings.py",
                "symbol": "old_name",
                "new_name": "new_name",
                "workspace": str(tmp_path),
            },
            edit_scope=guard,
        )
    )

    assert out.success, out.error
    assert out.data["files_changed"] == 2
    assert defining.read_text().startswith("Y")
    assert other.read_text().startswith("Y")


@pytest.mark.asyncio
async def test_rename_no_scope_preserves_unrestricted_behavior(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A None scope (no server-supplied authority) must not restrict the rename,
    # even across modules - exactly the prior public behavior.
    (tmp_path / "config").mkdir()
    (tmp_path / "agents").mkdir()
    defining = tmp_path / "config" / "settings.py"
    cross = tmp_path / "agents" / "models.py"
    defining.write_text("old_name = 1\n", encoding="utf-8")
    cross.write_text("old_name = 1\n", encoding="utf-8")

    _install_fake_lsp(monkeypatch, [defining, cross])

    out = await RenameSymbolTool().run(
        ToolInput(
            params={
                "path": "config/settings.py",
                "symbol": "old_name",
                "new_name": "new_name",
                "workspace": str(tmp_path),
            }
        )  # no edit_scope
    )

    assert out.success, out.error
    assert out.data["files_changed"] == 2
    assert defining.read_text().startswith("Y")
    assert cross.read_text().startswith("Y")


@pytest.mark.asyncio
async def test_rename_ignores_scope_forged_in_params(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A scope planted in the model-controlled params must have no effect: only the
    # denying guard on ToolInput.edit_scope decides. Here params carries a
    # permissive forgery while edit_scope forbids the cross-module file.
    (tmp_path / "config").mkdir()
    (tmp_path / "agents").mkdir()
    defining = tmp_path / "config" / "settings.py"
    forbidden = tmp_path / "agents" / "models.py"
    defining.write_text("old_name = 1\n", encoding="utf-8")
    forbidden.write_text("old_name = 1\n", encoding="utf-8")

    _install_fake_lsp(monkeypatch, [defining, forbidden])
    denying = _guard(tmp_path, TaskEditScope(modules=("platform.config",)))
    forged = build_guard(TaskEditScope(modules=("application.agents", "platform.config")), tmp_path)

    out = await RenameSymbolTool().run(
        ToolInput(
            params={
                "path": "config/settings.py",
                "symbol": "old_name",
                "new_name": "new_name",
                "workspace": str(tmp_path),
                "edit_scope": forged,  # untrusted - ignored
            },
            edit_scope=denying,
        )
    )

    assert not out.success
    assert out.failure_kind == "refused"
    assert forbidden.read_text() == "old_name = 1\n"


# --------------------------------------------------------------------------- #
# Orchestrator direct single-tool dispatch propagation                         #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_direct_dispatch_stamps_scope_onto_tool_input() -> None:
    """The Orchestrator's direct single-tool path must copy the task's server-owned
    scope onto ToolInput.edit_scope (never params) so a directly-routed mutating
    tool enforces the same scope it would inside an agent."""
    from orchestrator.orchestrator import Orchestrator

    guard = build_guard(TaskEditScope(modules=("platform.config",)), Path("."))

    captured: dict[str, object] = {}

    class _RecordingTool:
        is_mutating = False

        async def run(self, tool_input: ToolInput):
            captured["edit_scope"] = tool_input.edit_scope
            captured["params"] = dict(tool_input.params)
            return MagicMock(success=True, data={}, error=None)

        def format_output(self, _data):
            return "ok"

    registry = MagicMock()
    registry.get.return_value = _RecordingTool()

    orch = Orchestrator.__new__(Orchestrator)  # bypass heavy __init__; exercise one method
    orch._tool_registry = registry
    orch._running_task_store = None
    orch._stream_manager = MagicMock()
    orch._stream_manager.emit = _async_noop
    orch._journal = MagicMock()
    orch._journal.record = _async_noop
    orch._finish_task = _async_noop  # terminal bookkeeping is out of scope for this test

    plan = MagicMock()
    plan.direct_tool = "write_file"
    plan.direct_tool_params = {"path": "config/settings.py", "content": "V = 1\n"}

    await orch._execute_single_tool("t-direct", "prompt", plan, workspace=".", context="", edit_scope=guard)

    # The scope reached the tool via the dedicated field, not the model-facing params.
    assert captured["edit_scope"] is guard
    assert "edit_scope" not in captured["params"]


@pytest.mark.asyncio
async def test_direct_dispatch_none_scope_leaves_input_unrestricted() -> None:
    from orchestrator.orchestrator import Orchestrator

    captured: dict[str, object] = {}

    class _RecordingTool:
        is_mutating = False

        async def run(self, tool_input: ToolInput):
            captured["edit_scope"] = tool_input.edit_scope
            return MagicMock(success=True, data={}, error=None)

        def format_output(self, _data):
            return "ok"

    registry = MagicMock()
    registry.get.return_value = _RecordingTool()

    orch = Orchestrator.__new__(Orchestrator)
    orch._tool_registry = registry
    orch._running_task_store = None
    orch._stream_manager = MagicMock()
    orch._stream_manager.emit = _async_noop
    orch._journal = MagicMock()
    orch._journal.record = _async_noop
    orch._finish_task = _async_noop

    plan = MagicMock()
    plan.direct_tool = "write_file"
    plan.direct_tool_params = {"path": "x", "content": "y"}

    await orch._execute_single_tool("t-direct", "prompt", plan, workspace=".", context="")

    assert captured["edit_scope"] is None


async def _async_noop(*_args, **_kwargs):
    return None
