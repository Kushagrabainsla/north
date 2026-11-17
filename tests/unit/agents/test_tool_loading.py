"""North finds its own tools (issue #82, item 3).

Only the loop controls were always loaded; the rest came from a similarity search.
In chat tasks, calls to bash, fetch_url, web_search, gh and take_screenshot failed
"Tool 'X' not found", and north then told the user it had no shell. Now the four
most-used tools are always loaded, and a call to any other registered tool loads
it and runs it, with the same gates. A flow step keeps to its tool list.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from agents.capabilities import build_platform_capabilities_summary
from agents.models import AgentConfig, AgentDependencies, AgentPayload
from approval.approvals import Decision
from approval.policy import Verdict
from inference.models import ToolCall
from memory import FileContextStore
from tests.conftest import MockInferenceRouter
from tools.base import Tool
from tools.confidence import ConfidenceTracker
from tools.models import ToolInput, ToolOutput
from tools.registry import ToolRegistry

AGENTS_DIR = Path(__file__).parent.parent.parent.parent / "agents"
CORE_TOOLS = {"bash", "write_file", "fetch_url", "web_search"}


def _agent(tmp_path: Path, registry=None):
    import importlib

    config = AgentConfig.from_yaml(AGENTS_DIR / "north" / "config.yaml")
    cls = getattr(importlib.import_module("agents.north.agent"), config.resolved_class_name)
    deps = AgentDependencies(
        context_store=FileContextStore(tmp_path / "context"),
        inference_router=MockInferenceRouter(),
        tool_registry=registry or ToolRegistry(auto_register=False),
        confidence_tracker=ConfidenceTracker(db_path=tmp_path / "tools.db"),
    )
    return cls(config, deps)


class _Recorder(Tool):
    """A tool that records whether it ran."""

    description = "Record that it ran."
    parameters_schema = {"type": "object", "properties": {}}

    def __init__(self, name: str, *, mutating: bool = False) -> None:
        self.name = name
        self.is_mutating = mutating
        self.called = False

    async def run(self, input: ToolInput) -> ToolOutput:
        self.called = True
        return ToolOutput(success=True, data={"ran": self.name})


def _registry(*tools: Tool) -> ToolRegistry:
    registry = ToolRegistry(auto_register=False)
    for tool in tools:
        registry.register(tool)
    return registry


def _call(name: str) -> ToolCall:
    return ToolCall(name=name, call_id=f"{name}-1", params={})


async def test_the_four_most_used_tools_are_loaded_when_the_search_misses_them(tmp_path: Path) -> None:
    def _tool(name: str):
        tool = MagicMock()
        tool.name = name
        tool.description = f"Perform {name.replace('_', ' ')} operations"
        return tool

    names = [*CORE_TOOLS, "update_plan", "kasa", "take_photo", "schedule_task", "gh", "take_screenshot", "patch_file"]
    agent = _agent(tmp_path)
    agent._deps.tool_registry = MagicMock()
    agent._deps.tool_registry.available_tools.return_value = [_tool(name) for name in names]
    agent._deps.confidence_tracker = MagicMock()
    agent._deps.confidence_tracker.scores_for_agent = AsyncMock(return_value=[])
    index = MagicMock()
    index.update_tools = AsyncMock(return_value=0)
    index.search_tools = AsyncMock(return_value=["take_photo"])
    agent._deps.tool_index = index

    loaded = {tool.name for tool, _ in await agent._load_tools(AgentPayload(task_id="t", prompt="can you record?"))}

    assert loaded == CORE_TOOLS | {"update_plan", "take_photo"}


async def test_a_flow_step_does_not_get_the_four_tools_it_does_not_list(tmp_path: Path) -> None:
    agent = _agent(tmp_path, _registry(*(_Recorder(name) for name in CORE_TOOLS)))
    payload = AgentPayload(task_id="step", prompt="search", allowed_tools=["web_search"])

    loaded = {tool.name for tool, _ in await agent._load_tools(payload)}

    assert loaded == {"web_search"}


async def test_a_registered_tool_that_was_not_loaded_runs_when_called(tmp_path: Path) -> None:
    gh = _Recorder("gh")
    agent = _agent(tmp_path, _registry(gh))
    tool_map: dict[str, Tool] = {}

    _call_made, result, success, _ = await agent._safe_execute_call(
        _call("gh"), AgentPayload(task_id="chat", prompt="create an issue"), tool_map
    )

    assert success is True
    assert gh.called is True
    assert json.loads(result)["data"] == {"ran": "gh"}
    assert tool_map["gh"] is gh, "loaded for the next turns too, so the model sees its schema"


async def test_a_tool_loaded_on_call_goes_to_the_approval_layer(tmp_path: Path) -> None:
    """Loading on call is not a way round approvals: Tool.execute still decides a change."""
    tool = _Recorder("change_external_state", mutating=True)
    tool.approvals = MagicMock()
    tool.approvals.decide = AsyncMock(return_value=Decision(Verdict.REFUSE, "test", status=None))
    agent = _agent(tmp_path, _registry(tool))

    _call_made, _result, success, _ = await agent._safe_execute_call(
        _call(tool.name), AgentPayload(task_id="chat", prompt="change it"), {}
    )

    assert success is False
    assert tool.called is False
    tool.approvals.decide.assert_awaited_once()


async def test_the_read_only_profile_blocks_a_change_loaded_on_call(tmp_path: Path) -> None:
    """The tool is loaded before the gates, so the read-only gate sees that it changes something."""
    tool = _Recorder("write_artifact", mutating=True)
    agent = _agent(tmp_path, _registry(tool))
    payload = AgentPayload(task_id="quick", prompt="inspect", execution_profile="quick_readonly")

    _call_made, result, success, _ = await agent._safe_execute_call(_call(tool.name), payload, {})

    assert success is False
    assert "unavailable in the quick read-only profile" in result
    assert tool.called is False


async def test_a_flow_step_cannot_load_a_tool_outside_its_list(tmp_path: Path) -> None:
    outside = _Recorder("fetch_url")
    listed = _Recorder("web_search")
    agent = _agent(tmp_path, _registry(outside, listed))
    tool_map: dict[str, Tool] = {"web_search": listed}
    payload = AgentPayload(task_id="step", prompt="brief", allowed_tools=["web_search"])

    _call_made, result, success, _ = await agent._safe_execute_call(_call("fetch_url"), payload, tool_map)

    assert success is False
    assert outside.called is False
    assert "fetch_url" not in tool_map
    assert "This flow step can use only: web_search." in json.loads(result)["error"]


async def test_a_retired_tool_name_points_to_its_replacement(tmp_path: Path) -> None:
    agent = _agent(tmp_path, _registry(_Recorder("bash")))

    _call_made, result, success, _ = await agent._safe_execute_call(
        _call("read_file"), AgentPayload(task_id="chat", prompt="read it"), {}
    )

    assert success is False
    assert json.loads(result)["error"] == "Tool 'read_file' was removed: use 'bash' instead."


async def test_a_name_north_does_not_have_points_to_find_tools(tmp_path: Path) -> None:
    agent = _agent(tmp_path)

    _call_made, result, success, _ = await agent._safe_execute_call(
        _call("does_not_exist"), AgentPayload(task_id="chat", prompt="x"), {}
    )

    error = json.loads(result)["error"]
    assert success is False
    assert "Tool 'does_not_exist' not found." in error
    assert "find_tools" in error


async def test_an_internal_tool_name_is_not_taken_from_the_registry(tmp_path: Path) -> None:
    """ask_user and its kind are the loop's own: a registered tool of that name never replaces them."""
    impostor = _Recorder("ask_user")
    agent = _agent(tmp_path, _registry(impostor))
    tool_map: dict[str, Tool] = {}

    agent._load_called_tool("ask_user", tool_map, AgentPayload(task_id="chat", prompt="x"))

    assert tool_map == {}


def test_the_prompt_says_a_listed_tool_can_be_called_directly(tmp_path: Path) -> None:
    deps = MagicMock()
    deps.tool_registry.all_tools.return_value = [_Recorder("gh")]
    deps.flow_registry = None

    summary = build_platform_capabilities_summary(deps)

    assert "Call any tool named here directly" in summary
    assert "except in a flow step" in summary
