"""Tests for trusted learned-tool validation and approval gating."""

from __future__ import annotations

from pathlib import Path

import pytest

from policies.self_edit import SelfEditPolicy
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
    async def test_create_refused_without_approval_store(self) -> None:
        tool = CreateToolTool(tool_registry=None, approval_store=None)
        result = await tool.run(ToolInput(params={"action": "create", "name": "evil_tool", "content": _BENIGN_TOOL}))
        assert result.success is False
        assert "fail closed" in result.error

    async def test_update_refused_without_approval_store(self) -> None:
        tool = CreateToolTool(tool_registry=None, approval_store=None)
        result = await tool.run(ToolInput(params={"action": "update", "name": "read_file", "content": _BENIGN_TOOL}))
        assert result.success is False
        assert "fail closed" in result.error

    async def test_list_and_read_still_work_without_store(self) -> None:
        tool = CreateToolTool(tool_registry=None, approval_store=None)
        result = await tool.run(ToolInput(params={"action": "list"}))
        assert result.success is True


def test_learned_tool_is_created_and_core_tool_is_not_updateable(tmp_path: Path) -> None:
    learned = tmp_path / "learned" / "tools"
    policy = SelfEditPolicy(learned, tmp_path / "mutations")
    tool = CreateToolTool(tools_dir=learned, self_edit_policy=policy)

    created = tool._create(
        {
            "name": "learned_demo",
            "description": "demo",
            "content": _BENIGN_TOOL.replace("my_tool", "learned_demo"),
            "tool_type": "specialized",
        }
    )
    assert created.success
    assert (learned / "specialized" / "learned_demo.py").exists()

    core_update = tool._update(
        {
            "name": "read_file",
            "content": _BENIGN_TOOL.replace("my_tool", "read_file"),
        }
    )
    assert not core_update.success
    assert "outside the managed root" in core_update.error
