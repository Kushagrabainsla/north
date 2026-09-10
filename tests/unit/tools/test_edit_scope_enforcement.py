"""Runtime edit-scope enforcement in the mutating file tools.

Stage 2 gives a task a server-owned :class:`~architecture.scopes.TaskEditScope`.
These tests exercise the *runtime* half: the scope, bound into a
:class:`~architecture.scopes.ScopeGuard`, is carried on ``ToolInput.edit_scope``
(never in the model-controlled ``params``) and is consulted by WriteFileTool and
PatchFileTool before any bytes are written.

The guard is bound to a temporary workspace whose subdirectories mirror the real
module layout (``config/`` → ``platform.config``, ``agents/`` → ``application.agents``,
``architecture/`` → the protected ``architecture`` module) so the shipped module
manifest classifies the paths exactly as it would in the repo.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agents.models import AgentPayload
from architecture.scopes import ScopeGuard, TaskEditScope, build_guard
from orchestrator.models import TaskRequest
from tools.models import ToolInput
from tools.specialized.patch_file import PatchFileTool
from tools.universal.write_file import WriteFileTool


def _seed(workspace: Path, relative: str, content: str) -> Path:
    """Create a file at *relative* under *workspace* and return it."""
    path = workspace / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _mark_read(task_id: str, workspace: Path, relative: str) -> None:
    """Record the read using the same resolved path the tool will compute.

    PatchFileTool checks ``was_read(task_id, str(resolve_path(...)))``; on macOS a
    tmp path can be a symlink whose resolved form differs from the raw string, so
    recording the raw path would spuriously fail the read-precondition.
    """
    from tools._path import resolve_path
    from tools._read_tracker import record_read

    resolved = resolve_path(relative, str(workspace))
    assert resolved is not None
    record_read(task_id, str(resolved))


def _guard(workspace: Path, scope: TaskEditScope) -> ScopeGuard:
    return build_guard(scope, workspace)


# --------------------------------------------------------------------------- #
# WriteFileTool                                                               #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_write_permitted_edit_within_scope(tmp_path: Path) -> None:
    guard = _guard(tmp_path, TaskEditScope(modules=("platform.config",)))
    out = await WriteFileTool().run(
        ToolInput(
            params={"path": "config/settings.py", "content": "VALUE = 1\n", "workspace": str(tmp_path)},
            edit_scope=guard,
        )
    )
    assert out.success, out.error
    assert (tmp_path / "config/settings.py").read_text() == "VALUE = 1\n"


@pytest.mark.asyncio
async def test_write_denied_cross_module_edit(tmp_path: Path) -> None:
    guard = _guard(tmp_path, TaskEditScope(modules=("platform.config",)))
    out = await WriteFileTool().run(
        ToolInput(
            params={"path": "agents/models.py", "content": "x = 1\n", "workspace": str(tmp_path)},
            edit_scope=guard,
        )
    )
    assert not out.success
    assert out.failure_kind == "refused"
    assert "outside this task's permitted modules and paths" in (out.error or "")
    assert not (tmp_path / "agents/models.py").exists()  # no bytes written


@pytest.mark.asyncio
async def test_write_protected_path_requires_explicit_authorization(tmp_path: Path) -> None:
    # `architecture/**` is a protected module; module membership alone is not enough.
    unauthorized = _guard(tmp_path, TaskEditScope(modules=("architecture",)))
    out = await WriteFileTool().run(
        ToolInput(
            params={"path": "architecture/scopes.py", "content": "x = 1\n", "workspace": str(tmp_path)},
            edit_scope=unauthorized,
        )
    )
    assert not out.success
    assert "requires explicit user authorization" in (out.error or "")

    authorized = _guard(
        tmp_path,
        TaskEditScope(modules=("architecture",), authorized_paths=("architecture/scopes.py",)),
    )
    out = await WriteFileTool().run(
        ToolInput(
            params={"path": "architecture/scopes.py", "content": "x = 2\n", "workspace": str(tmp_path)},
            edit_scope=authorized,
        )
    )
    assert out.success, out.error
    assert (tmp_path / "architecture/scopes.py").read_text() == "x = 2\n"


@pytest.mark.asyncio
async def test_write_no_scope_preserves_unrestricted_behavior(tmp_path: Path) -> None:
    # A None scope (every current public caller) must not restrict edits.
    out = await WriteFileTool().run(
        ToolInput(params={"path": "agents/models.py", "content": "x = 1\n", "workspace": str(tmp_path)})
    )
    assert out.success, out.error
    assert (tmp_path / "agents/models.py").read_text() == "x = 1\n"


@pytest.mark.asyncio
async def test_write_scope_ignores_model_supplied_params(tmp_path: Path) -> None:
    # A scope forged in the model-controlled params must have no effect: the
    # denying guard on ToolInput.edit_scope is the only authority.
    guard = _guard(tmp_path, TaskEditScope(modules=("platform.config",)))
    forged = build_guard(TaskEditScope(modules=("application.agents",)), tmp_path)
    out = await WriteFileTool().run(
        ToolInput(
            params={
                "path": "agents/models.py",
                "content": "x = 1\n",
                "workspace": str(tmp_path),
                "edit_scope": forged,  # ignored - params are untrusted
            },
            edit_scope=guard,
        )
    )
    assert not out.success
    assert out.failure_kind == "refused"


# --------------------------------------------------------------------------- #
# PatchFileTool                                                               #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_patch_permitted_edit_within_scope(tmp_path: Path) -> None:
    _seed(tmp_path, "config/settings.py", "VALUE = 1\n")
    guard = _guard(tmp_path, TaskEditScope(modules=("platform.config",)))
    task_id = "task-permit"
    _mark_read(task_id, tmp_path, "config/settings.py")
    out = await PatchFileTool().run(
        ToolInput(
            params={
                "path": "config/settings.py",
                "old_string": "VALUE = 1",
                "new_string": "VALUE = 2",
                "workspace": str(tmp_path),
                "task_id": task_id,
            },
            edit_scope=guard,
        )
    )
    assert out.success, out.error
    assert (tmp_path / "config/settings.py").read_text() == "VALUE = 2\n"


@pytest.mark.asyncio
async def test_patch_denied_cross_module_edit_before_mutation(tmp_path: Path) -> None:
    original = "x = 1\n"
    _seed(tmp_path, "agents/models.py", original)
    guard = _guard(tmp_path, TaskEditScope(modules=("platform.config",)))
    out = await PatchFileTool().run(
        ToolInput(
            params={
                "path": "agents/models.py",
                "old_string": "x = 1",
                "new_string": "x = 2",
                "workspace": str(tmp_path),
                "task_id": "task-deny",
            },
            edit_scope=guard,
        )
    )
    assert not out.success
    assert out.failure_kind == "refused"
    assert "outside this task's permitted modules and paths" in (out.error or "")
    assert (tmp_path / "agents/models.py").read_text() == original  # unchanged


@pytest.mark.asyncio
async def test_patch_protected_path_requires_explicit_authorization(tmp_path: Path) -> None:
    _seed(tmp_path, "architecture/scopes.py", "x = 1\n")
    task_id = "task-protected"
    _mark_read(task_id, tmp_path, "architecture/scopes.py")

    unauthorized = _guard(tmp_path, TaskEditScope(modules=("architecture",)))
    out = await PatchFileTool().run(
        ToolInput(
            params={
                "path": "architecture/scopes.py",
                "old_string": "x = 1",
                "new_string": "x = 2",
                "workspace": str(tmp_path),
                "task_id": task_id,
            },
            edit_scope=unauthorized,
        )
    )
    assert not out.success
    assert "requires explicit user authorization" in (out.error or "")
    assert (tmp_path / "architecture/scopes.py").read_text() == "x = 1\n"

    authorized = _guard(
        tmp_path,
        TaskEditScope(modules=("architecture",), authorized_paths=("architecture/scopes.py",)),
    )
    out = await PatchFileTool().run(
        ToolInput(
            params={
                "path": "architecture/scopes.py",
                "old_string": "x = 1",
                "new_string": "x = 2",
                "workspace": str(tmp_path),
                "task_id": task_id,
            },
            edit_scope=authorized,
        )
    )
    assert out.success, out.error
    assert (tmp_path / "architecture/scopes.py").read_text() == "x = 2\n"


@pytest.mark.asyncio
async def test_patch_no_scope_preserves_unrestricted_behavior(tmp_path: Path) -> None:
    _seed(tmp_path, "agents/models.py", "x = 1\n")
    task_id = "task-none"
    _mark_read(task_id, tmp_path, "agents/models.py")
    out = await PatchFileTool().run(
        ToolInput(
            params={
                "path": "agents/models.py",
                "old_string": "x = 1",
                "new_string": "x = 2",
                "workspace": str(tmp_path),
                "task_id": task_id,
            }
        )
    )
    assert out.success, out.error
    assert (tmp_path / "agents/models.py").read_text() == "x = 2\n"


# --------------------------------------------------------------------------- #
# Server-owned propagation                                                    #
# --------------------------------------------------------------------------- #


def test_scope_field_excluded_from_serialization() -> None:
    # A dumped ToolInput never carries the scope - it is not model-facing data.
    guard = build_guard(TaskEditScope(modules=("platform.config",)), Path("."))
    assert "edit_scope" not in ToolInput(params={"path": "x"}, edit_scope=guard).model_dump()
    assert "edit_scope" not in AgentPayload(task_id="t", prompt="p", edit_scope=guard).model_dump()
    assert "edit_scope" not in TaskRequest(prompt="p", edit_scope=guard).model_dump()


def test_scope_propagates_request_to_payload_shape() -> None:
    # The same guard object rides the request and the payload unchanged; the
    # orchestrator copies request.edit_scope onto every AgentPayload it builds.
    guard = build_guard(TaskEditScope(modules=("platform.config",)), Path("."))
    request = TaskRequest(prompt="p", edit_scope=guard)
    payload = AgentPayload(task_id="t", prompt=request.prompt, edit_scope=request.edit_scope)
    assert payload.edit_scope is guard
    # And a plain request leaves it unrestricted (public-caller default).
    assert TaskRequest(prompt="p").edit_scope is None
    assert AgentPayload(task_id="t", prompt="p").edit_scope is None


@pytest.mark.asyncio
async def test_agent_execute_call_threads_scope_to_tool(tmp_path: Path) -> None:
    """End-to-end runtime propagation: a denying scope on the AgentPayload reaches
    WriteFileTool through the agent's own dispatch path and blocks the write."""
    from unittest.mock import MagicMock

    from agents.models import AgentDependencies
    from agents.researcher.agent import ResearcherAgent
    from inference.models import ToolCall
    from memory import FileContextStore
    from tools.confidence import ConfidenceTracker
    from tools.registry import ToolRegistry

    deps = AgentDependencies(
        context_store=FileContextStore(tmp_path / "context"),
        inference_router=MagicMock(),
        tool_registry=ToolRegistry(graph={}, auto_register=False),
        confidence_tracker=ConfidenceTracker(db_path=tmp_path / "tools.db"),
    )
    from agents.models import AgentConfig

    agent = ResearcherAgent(
        AgentConfig(agent="researcher", domain="engineering"),
        deps,
    )
    tool_map = {"write_file": WriteFileTool()}
    guard = _guard(tmp_path, TaskEditScope(modules=("platform.config",)))
    payload = AgentPayload(task_id="t-prop", prompt="p", workspace=str(tmp_path), edit_scope=guard)

    call = ToolCall(
        call_id="c1",
        name="write_file",
        params={"path": "agents/models.py", "content": "x = 1\n"},
    )
    _call, result_str, success, _images = await agent._execute_call(call, payload, tool_map)
    assert not success
    assert "outside this task's permitted modules and paths" in result_str
    assert not (tmp_path / "agents/models.py").exists()

    # The same call under a permitting scope goes through.
    allow = _guard(tmp_path, TaskEditScope(modules=("platform.config",)))
    ok_call = ToolCall(call_id="c2", name="write_file", params={"path": "config/settings.py", "content": "V = 1\n"})
    ok_payload = AgentPayload(task_id="t-prop", prompt="p", workspace=str(tmp_path), edit_scope=allow)
    _c, _s, ok_success, _i = await agent._execute_call(ok_call, ok_payload, tool_map)
    assert ok_success, _s
    assert (tmp_path / "config/settings.py").read_text() == "V = 1\n"
