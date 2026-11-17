"""Tests for tool discovery and prompt/registry drift (review finding R5#27)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tools.registry import ToolRegistry

AGENTS_DIR = Path(__file__).resolve().parents[3] / "agents"

# Pseudo-tools implemented by the agent loop itself, not the registry.
_AGENT_BUILTINS = {"request_approval", "ask_user"}
# Tools that need constructor dependencies and are registered manually in
# orchestrator/app.py::_build_tool_registry rather than auto-discovered.
_MANUALLY_REGISTERED = {
    "bash",
    "schedule_task",
    "list_schedules",
    "update_schedule",
    "cancel_schedule",
    "query_metrics",
}

# Matches tool-call syntax in prompt code examples: name(param=... or name(\n  param=
_TOOL_CALL_RE = re.compile(r"\b([a-z][a-z0-9_]{2,})\(\s*\n?\s*[a-z_]+\s*=", re.MULTILINE)


def _registry_names() -> set[str]:
    registry = ToolRegistry(auto_register=True)
    return {tool.name for tool in registry.all_tools()} | _MANUALLY_REGISTERED


def test_the_retired_file_tools_are_gone_and_write_file_stays() -> None:
    """The shell reads, lists and searches files (issue #78); only the write tool, with its diff card, is left."""
    from utils.retired_tools import RETIRED_TOOLS

    registry = ToolRegistry(auto_register=True)
    available = {t.name for t in registry.available_tools(auto_reload=False)}
    assert not available & set(RETIRED_TOOLS)
    assert "write_file" in available


@pytest.mark.parametrize(
    "prompt_path",
    sorted(AGENTS_DIR.glob("*/prompts/system.md")),
    ids=lambda p: p.parent.parent.name,
)
def test_every_tool_named_in_agent_prompts_resolves(prompt_path: Path) -> None:
    """Prompt/registry drift gate: a system prompt must never instruct an agent
    to call a tool that does not resolve in the registry."""
    content = prompt_path.read_text(encoding="utf-8")
    referenced = set(_TOOL_CALL_RE.findall(content)) - _AGENT_BUILTINS
    unknown = referenced - _registry_names()
    assert not unknown, f"{prompt_path} references unregistered tools: {sorted(unknown)}"


def _declared_tool_names(path: Path) -> list[str]:
    """The `name = "..."` each Tool class in *path* declares, read without importing it."""
    import ast

    names = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if (
                    isinstance(item, ast.Assign)
                    and any(isinstance(target, ast.Name) and target.id == "name" for target in item.targets)
                    and isinstance(item.value, ast.Constant)
                    and isinstance(item.value.value, str)
                ):
                    names.append(item.value.value)
    return names


@pytest.mark.parametrize("folder", ["universal", "specialized"])
def test_a_built_in_tools_file_is_named_after_the_tool(folder: str) -> None:
    """The dashboard and create_tool find a tool's code as `<name>.py`: git, gh and kasa lived in
    `<name>_tool.py`, so their pages answered 404 and create_tool could not read or update them."""
    tools_dir = Path(__file__).resolve().parents[3] / "tools" / folder
    misnamed = {
        path.name: names
        for path in sorted(tools_dir.glob("*.py"))
        if not path.name.startswith("_") and (names := _declared_tool_names(path)) and path.stem not in names
    }
    assert not misnamed, f"rename these files to their tool's name: {misnamed}"
