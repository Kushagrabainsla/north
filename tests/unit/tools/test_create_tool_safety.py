"""Tests for CreateToolTool safety hardening (review finding R2#12)."""

from __future__ import annotations

import pytest

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
            "import importlib\nimportlib.import_module('os')",
            "from importlib import import_module",
            "import builtins",
            "import runpy",
            "import subprocess",
            "import os",
            "x = open('/etc/passwd').read()",
            "f = getattr(__builtins__, 'open')",
            "exec('print(1)')",
            "eval('1+1')",
            "__import__('os')",
            "x = (1).__class__.__subclasses__()",
            "g = globals()",
        ],
    )
    def test_escape_hatches_rejected(self, snippet: str) -> None:
        safe, reason = _check_code_safety(snippet)
        assert not safe, f"should have rejected: {snippet}"
        assert reason


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
