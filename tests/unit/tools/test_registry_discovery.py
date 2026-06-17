"""Tests for tool discovery and prompt/registry drift (review finding R5#27)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tools.registry import ToolRegistry

AGENTS_DIR = Path(__file__).resolve().parents[3] / "agents"

# Pseudo-tools implemented by the agent loop itself, not the registry.
_AGENT_BUILTINS = {"delegate_task", "request_approval", "ask_user"}
# Tools that need constructor dependencies and are registered manually in
# orchestrator/app.py::_build_tool_registry rather than auto-discovered.
_MANUALLY_REGISTERED = {"bash", "shell", "schedule_task", "create_agent", "query_metrics"}

# Matches tool-call syntax in prompt code examples: name(param=... or name(\n  param=
_TOOL_CALL_RE = re.compile(r"\b([a-z][a-z0-9_]{2,})\(\s*\n?\s*[a-z_]+\s*=", re.MULTILINE)


def _registry_names() -> set[str]:
    registry = ToolRegistry(auto_register=True)
    return {tool.name for tool in registry.all_tools()} | _MANUALLY_REGISTERED


def test_analysis_and_semantic_tools_are_discovered() -> None:
    registry = ToolRegistry(auto_register=True)
    names = {tool.name for tool in registry.all_tools()}
    assert {"check_types", "search_symbols", "find_references"} <= names


def test_coding_tools_are_universal() -> None:
    """The coder prompt's verify-after-every-edit loop depends on these resolving
    for the engineering agents."""
    registry = ToolRegistry(auto_register=True)
    coder_tools = {t.name for t in registry.tools_for_agent("coder", auto_reload=False)}
    assert {"check_types", "search_symbols", "find_references", "read_file", "list_dir"} <= coder_tools


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
