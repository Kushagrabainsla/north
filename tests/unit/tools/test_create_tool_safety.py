"""Tests for trusted learned-tool validation and approval gating."""

from __future__ import annotations

from pathlib import Path

import pytest

from policies.self_edit import SelfEditPolicy
from tools.base import Tool
from tools.models import ToolInput
from tools.universal.create_tool import CreateToolTool, _check_code_safety

_BENIGN_TOOL = """
from tools.base import Tool
from tools.models import ToolInput, ToolOutput

class MyTool(Tool):
    name = "my_tool"
    description = "demo"
    parameters_schema = {"type": "object", "properties": {}}
    def format_output(self, data: dict) -> str:
        return "ok"
    async def run(self, input: ToolInput) -> ToolOutput:
        return ToolOutput(success=True, data={})
"""


class TestCodeSafetyCheck:
    def test_benign_tool_accepted(self) -> None:
        safe, reason = _check_code_safety(_BENIGN_TOOL)
        assert safe, reason

    @pytest.mark.parametrize(
        "snippet",
        [
            "import subprocess\nsubprocess.run(['true'])",
            "import socket\nsocket.socket()",
            "from pathlib import Path\nPath('/tmp/x').write_text('x')",
            "import os\nos.environ.get('PATH')",
            "value = getattr(object(), 'missing', None)",
            "exec('value = 1')",
            "eval('1 + 1')",
        ],
    )
    def test_trusted_capabilities_are_accepted(self, snippet: str) -> None:
        safe, reason = _check_code_safety(snippet)
        assert safe, f"trusted extension should be accepted: {reason}"

    def test_malformed_source_is_rejected(self) -> None:
        safe, reason = _check_code_safety("def broken(:\n    pass")
        assert not safe
        assert "Syntax error" in reason


class TestFailClosedGate:
    async def test_create_refused_without_an_approval_layer(self) -> None:
        tool = CreateToolTool(tool_registry=None)
        result = await tool.execute(
            ToolInput(params={"action": "create", "name": "evil_tool", "content": _BENIGN_TOOL})
        )
        assert result.success is False
        assert "fail closed" in result.error

    async def test_update_refused_without_an_approval_layer(self) -> None:
        tool = CreateToolTool(tool_registry=None)
        result = await tool.execute(
            ToolInput(params={"action": "update", "name": "read_file", "content": _BENIGN_TOOL})
        )
        assert result.success is False
        assert "fail closed" in result.error

    async def test_list_and_read_still_work_without_an_approval_layer(self) -> None:
        tool = CreateToolTool(tool_registry=None)
        result = await tool.execute(ToolInput(params={"action": "list"}))
        assert result.success is True


class _Registry:
    def __init__(self) -> None:
        self.tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self.tools[tool.name] = tool

    def all_tool_names(self) -> set[str]:
        return set(self.tools)


def _demo(tmp_path: Path, **kw) -> tuple[CreateToolTool, _Registry, Path]:
    learned = tmp_path / "learned" / "tools"
    registry = _Registry()
    tool = CreateToolTool(
        tool_registry=registry, tools_dir=learned, self_edit_policy=SelfEditPolicy(learned, tmp_path / "m")
    )
    return tool, registry, learned


def _create(code: str, **params) -> ToolInput:
    return ToolInput(
        params={"action": "create", "name": "learned_demo", "content": code, "tool_type": "specialized", **params}
    )


async def test_one_call_writes_tests_and_switches_on_a_tool(tmp_path: Path) -> None:
    """One card covers writing, testing and switching on; there is no separate activation to ask for."""
    tool, registry, learned = _demo(tmp_path)

    out = await tool.run(_create(_BENIGN_TOOL.replace("my_tool", "learned_demo"), test_params={}))

    assert out.success, out.error
    assert (learned / "specialized" / "learned_demo.py").exists()
    assert not (learned / "candidates" / "specialized" / "learned_demo.py").exists()
    assert "learned_demo" in registry.tools


async def test_a_tool_whose_test_call_fails_stays_a_draft(tmp_path: Path) -> None:
    tool, registry, learned = _demo(tmp_path)
    broken = _BENIGN_TOOL.replace("my_tool", "learned_demo").replace(
        "ToolOutput(success=True, data={})", 'ToolOutput(success=False, error="the device is missing")'
    )

    out = await tool.run(_create(broken, test_params={}))

    assert not out.success and "the device is missing" in out.error and "stays a draft" in out.error
    assert (learned / "candidates" / "specialized" / "learned_demo.py").exists()
    assert "learned_demo" not in registry.tools


async def test_nothing_is_written_without_test_params(tmp_path: Path) -> None:
    tool, _registry, learned = _demo(tmp_path)

    out = await tool.run(_create(_BENIGN_TOOL.replace("my_tool", "learned_demo")))

    assert not out.success and "test_params" in out.error
    assert not (learned / "candidates" / "specialized" / "learned_demo.py").exists()


async def test_a_test_call_that_changes_something_asks_like_any_call(tmp_path: Path) -> None:
    """The tool's card covers writing and testing it, not what its test call does: recording the mic still asks."""
    from tests.conftest import bind_approvals, rejecting_store

    tool, registry, _learned = _demo(tmp_path)
    store = rejecting_store()
    bind_approvals(tool, store=store)
    mutating = _BENIGN_TOOL.replace("my_tool", "learned_demo").replace(
        'description = "demo"', 'description = "demo"\n    is_mutating = True'
    )

    out = await tool.run(_create(mutating, test_params={}))

    assert not out.success and "stays a draft" in out.error
    store.add.assert_called_once()
    assert "learned_demo" not in registry.tools


async def test_a_draft_already_there_is_tested_and_switched_on_by_activate(tmp_path: Path) -> None:
    tool, registry, _learned = _demo(tmp_path)
    assert tool._update({"name": "read_file", "content": _BENIGN_TOOL.replace("my_tool", "read_file")}).success

    out = await tool.run(ToolInput(params={"action": "activate", "name": "read_file", "test_params": {}}))

    assert out.success, out.error
    assert "read_file" in registry.tools


async def test_an_approved_tool_change_is_not_replayed_for_different_code(tmp_path: Path) -> None:
    """One approval of `record_media` let any later code for it be switched on in safe mode without a card."""
    from approval.approval_memory import ApprovalMemory

    learned = tmp_path / "learned" / "tools"
    tool = CreateToolTool(tools_dir=learned, self_edit_policy=SelfEditPolicy(learned, tmp_path / "mutations"))
    memory = ApprovalMemory(tmp_path / "approval_memory.db")

    async def update_with(code: str):
        request = await tool.describe(ToolInput(params={"action": "update", "name": "my_tool", "content": code}))
        return request.action

    approved = await update_with(_BENIGN_TOOL)
    memory.record(approved.agent, approved.describe(), "approved")

    same = await update_with(_BENIGN_TOOL)
    changed = await update_with(_BENIGN_TOOL.replace("demo", "something else"))
    assert memory.recall(same.agent, same.describe()) == "approved"
    assert memory.recall(changed.agent, changed.describe()) is None


def test_updating_builtin_tool_stages_learned_candidate(tmp_path: Path) -> None:
    learned = tmp_path / "learned" / "tools"
    policy = SelfEditPolicy(learned, tmp_path / "mutations")
    tool = CreateToolTool(tools_dir=learned, self_edit_policy=policy)

    core_update = tool._update(
        {
            "name": "read_file",
            "content": _BENIGN_TOOL.replace("my_tool", "read_file"),
        }
    )
    assert core_update.success
    assert (learned / "candidates" / "universal" / "read_file.py").exists()
