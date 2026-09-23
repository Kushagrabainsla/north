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


class _Registry:
    def __init__(self) -> None:
        self.tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self.tools[tool.name] = tool

    def all_tool_names(self) -> set[str]:
        return set(self.tools)


async def test_tool_stays_candidate_until_validated_tested_and_activated(tmp_path: Path) -> None:
    learned = tmp_path / "learned" / "tools"
    policy = SelfEditPolicy(learned, tmp_path / "mutations")
    registry = _Registry()
    tool = CreateToolTool(tool_registry=registry, tools_dir=learned, self_edit_policy=policy)

    created = tool._create(
        {
            "name": "learned_demo",
            "description": "demo",
            "content": _BENIGN_TOOL.replace("my_tool", "learned_demo"),
            "tool_type": "specialized",
        }
    )
    assert created.success
    candidate = learned / "candidates" / "specialized" / "learned_demo.py"
    active = learned / "specialized" / "learned_demo.py"
    assert candidate.exists()
    assert not active.exists()
    assert "learned_demo" not in registry.tools

    refused = tool._activate({"name": "learned_demo", "user_confirmed": True})
    assert not refused.success
    assert "Test" in refused.error

    validated = tool._validate({"name": "learned_demo"})
    assert validated.success
    tested = await tool._test({"name": "learned_demo", "test_params": {}})
    assert tested.success

    unconfirmed = tool._activate({"name": "learned_demo"})
    assert not unconfirmed.success
    assert "confirmation" in unconfirmed.error

    activated = tool._activate({"name": "learned_demo", "user_confirmed": True})
    assert activated.success
    assert active.exists()
    assert not candidate.exists()
    assert "learned_demo" in registry.tools


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
