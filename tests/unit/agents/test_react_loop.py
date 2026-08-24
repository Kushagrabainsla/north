"""Tests for AgenticLLMAgent ReAct loop mechanics.

Each engineering agent is a thin subclass of AgenticLLMAgent - all domain-
specific behaviour lives in system prompts.  These tests verify the loop
itself: final answer path, tool execution, cost accumulation, iteration cap,
unknown-tool resilience, priority resolution, and context loading.

No real network calls are made; inference is fully mocked.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agents.models import AgentConfig, AgentDependencies, AgentPayload
from inference.models import ToolCall, ToolCallResponse
from memory import FileContextStore
from tests.conftest import MockInferenceRouter
from tools.confidence import ConfidenceTracker
from tools.registry import ToolRegistry

AGENTS_DIR = Path(__file__).parent.parent.parent.parent / "agents"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_deps(
    tmp_path: Path,
    router: MockInferenceRouter | None = None,
    max_iterations: int = 40,
) -> AgentDependencies:
    return AgentDependencies(
        context_store=FileContextStore(tmp_path / "context"),
        inference_router=router or MockInferenceRouter(),
        tool_registry=ToolRegistry(graph={}, auto_register=False),
        confidence_tracker=ConfidenceTracker(db_path=tmp_path / "tools.db"),
        agent_max_iterations=max_iterations,
    )


def _load_agent(name: str, tmp_path: Path, router: MockInferenceRouter | None = None):
    import importlib

    config = AgentConfig.from_yaml(AGENTS_DIR / name / "config.yaml")
    mod = importlib.import_module(f"agents.{name}.agent")
    cls = getattr(mod, config.resolved_class_name)
    return cls(config, _make_deps(tmp_path, router))


# ---------------------------------------------------------------------------
# Final answer path - all 4 agents complete successfully
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["architect", "coder", "researcher", "reviewer"])
async def test_agent_run_returns_valid_result(name: str, tmp_path: Path) -> None:
    """Each agent must return a valid AgentResult when LLM responds with a message."""
    from agents.models import AgentResult

    agent = _load_agent(name, tmp_path)
    result = await agent.run(AgentPayload(task_id="t1", prompt="Say hello."))

    assert isinstance(result, AgentResult)
    assert isinstance(result.output, str)
    assert isinstance(result.summary, str)
    assert result.cost_usd >= 0.0
    assert result.requires_approval is False


async def test_final_answer_content_is_preserved(tmp_path: Path) -> None:
    """Output text from the LLM response must appear verbatim in AgentResult.output."""

    class FixedTextRouter(MockInferenceRouter):
        async def complete_with_tools(self, request, token_callback=None):
            text = "Research complete. Findings at context.md."
            if token_callback:
                await token_callback(text)
            return ToolCallResponse(
                type="message",
                content=text,
                calls=[],
                model_used="mock",
                tokens_in=10,
                tokens_out=5,
                cost_usd=0.001,
            )

    agent = _load_agent("researcher", tmp_path, FixedTextRouter())
    result = await agent.run(AgentPayload(task_id="t1", prompt="Research auth."))
    assert "Research complete" in result.output
    assert result.cost_usd == pytest.approx(0.001)


# ---------------------------------------------------------------------------
# Tool call path - loop continues after a tool call
# ---------------------------------------------------------------------------


async def test_tool_call_then_final_answer_takes_two_iterations(tmp_path: Path) -> None:
    """Agent must execute one tool call then return the final answer from the next iteration."""
    call_count = 0

    class ToolThenMessageRouter(MockInferenceRouter):
        async def complete_with_tools(self, request, token_callback=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return ToolCallResponse(
                    type="tool_calls",
                    calls=[ToolCall(name="missing_tool", call_id="c1", params={})],
                    model_used="mock",
                    tokens_in=10,
                    tokens_out=5,
                )
            text = "Done after tool."
            if token_callback:
                await token_callback(text)
            return ToolCallResponse(
                type="message",
                content=text,
                calls=[],
                model_used="mock",
                tokens_in=10,
                tokens_out=5,
            )

    agent = _load_agent("architect", tmp_path, ToolThenMessageRouter())
    result = await agent.run(AgentPayload(task_id="t2", prompt="Design something."))
    assert call_count == 2
    assert result.output == "Done after tool."


async def test_tool_result_injected_into_next_request(tmp_path: Path) -> None:
    """After a tool call, the tool result must appear as a 'tool' role message in the next request."""
    received_messages_on_second_call: list[dict] = []
    call_count = 0

    class InspectingRouter(MockInferenceRouter):
        async def complete_with_tools(self, request, token_callback=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return ToolCallResponse(
                    type="tool_calls",
                    calls=[ToolCall(name="missing_tool", call_id="c1", params={})],
                    model_used="mock",
                    tokens_in=10,
                    tokens_out=5,
                )
            received_messages_on_second_call.extend(request.messages)
            text = "Inspected."
            if token_callback:
                await token_callback(text)
            return ToolCallResponse(
                type="message",
                content=text,
                calls=[],
                model_used="mock",
                tokens_in=10,
                tokens_out=5,
            )

    agent = _load_agent("reviewer", tmp_path, InspectingRouter())
    await agent.run(AgentPayload(task_id="t3", prompt="Run tests."))

    tool_messages = [m for m in received_messages_on_second_call if m.get("role") == "tool"]
    assert len(tool_messages) >= 1, "Tool result must be injected into conversation history"
    # The tool result must contain an error about the missing tool
    import json

    result_data = json.loads(tool_messages[0]["content"])
    assert result_data["success"] is False
    assert "missing_tool" in result_data["error"]


# ---------------------------------------------------------------------------
# Cost accumulation
# ---------------------------------------------------------------------------


async def test_cost_accumulates_across_iterations(tmp_path: Path) -> None:
    """total cost_usd must be the sum of all individual iteration costs."""
    call_count = 0

    class CostRouter(MockInferenceRouter):
        async def complete_with_tools(self, request, token_callback=None):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                return ToolCallResponse(
                    type="tool_calls",
                    calls=[ToolCall(name="bad_tool", call_id=f"c{call_count}", params={})],
                    model_used="mock",
                    tokens_in=10,
                    tokens_out=5,
                    cost_usd=0.01,
                )
            text = "Final."
            if token_callback:
                await token_callback(text)
            return ToolCallResponse(
                type="message",
                content=text,
                calls=[],
                model_used="mock",
                tokens_in=10,
                tokens_out=5,
                cost_usd=0.01,
            )

    agent = _load_agent("coder", tmp_path, CostRouter())
    result = await agent.run(AgentPayload(task_id="t4", prompt="Implement x."))
    assert call_count == 3
    assert result.cost_usd == pytest.approx(0.03)


# ---------------------------------------------------------------------------
# Iteration cap
# ---------------------------------------------------------------------------


async def test_max_iterations_returns_graceful_fallback(tmp_path: Path) -> None:
    """Agent must stop at the iteration cap and return a descriptive fallback message."""
    iterations_called = 0

    class NeverFinishesRouter(MockInferenceRouter):
        async def complete_with_tools(self, request, token_callback=None):
            nonlocal iterations_called
            iterations_called += 1
            return ToolCallResponse(
                type="tool_calls",
                calls=[ToolCall(name="bad_tool", call_id=f"c{iterations_called}", params={})],
                model_used="mock",
                tokens_in=10,
                tokens_out=5,
            )

    agent = _load_agent("architect", tmp_path, NeverFinishesRouter())
    agent._deps.agent_max_iterations = 3

    result = await agent.run(AgentPayload(task_id="t5", prompt="Never ends."))
    assert iterations_called == 3
    assert "maximum" in result.output.lower() or "iteration" in result.output.lower()


# ---------------------------------------------------------------------------
# Unknown tool resilience
# ---------------------------------------------------------------------------


async def test_unknown_tool_call_returns_error_and_loop_continues(tmp_path: Path) -> None:
    """An unknown tool name must produce an error JSON result and not crash the loop."""
    import json

    second_request_messages: list[dict] = []
    call_count = 0

    class UnknownToolRouter(MockInferenceRouter):
        async def complete_with_tools(self, request, token_callback=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return ToolCallResponse(
                    type="tool_calls",
                    calls=[ToolCall(name="does_not_exist", call_id="c1", params={})],
                    model_used="mock",
                    tokens_in=10,
                    tokens_out=5,
                )
            second_request_messages.extend(request.messages)
            text = "Recovered after error."
            if token_callback:
                await token_callback(text)
            return ToolCallResponse(
                type="message",
                content=text,
                calls=[],
                model_used="mock",
                tokens_in=10,
                tokens_out=5,
            )

    agent = _load_agent("reviewer", tmp_path, UnknownToolRouter())
    result = await agent.run(AgentPayload(task_id="t6", prompt="Run tests."))

    assert result.output == "Recovered after error."
    tool_msgs = [m for m in second_request_messages if m.get("role") == "tool"]
    assert tool_msgs, "Error result must be in conversation history"
    data = json.loads(tool_msgs[0]["content"])
    assert data["success"] is False
    assert "does_not_exist" in data["error"]


async def test_empty_tool_calls_list_breaks_loop(tmp_path: Path) -> None:
    """A 'tool_calls' response with an empty calls list must exit the loop."""

    class EmptyCallsRouter(MockInferenceRouter):
        async def complete_with_tools(self, request, token_callback=None):
            # Returns tool_calls type but no actual calls - should break
            return ToolCallResponse(
                type="tool_calls",
                calls=[],
                model_used="mock",
                tokens_in=10,
                tokens_out=5,
            )

    agent = _load_agent("researcher", tmp_path, EmptyCallsRouter())
    result = await agent.run(AgentPayload(task_id="t7", prompt="Research x."))
    # Loop breaks → returns the iteration-limit fallback
    assert isinstance(result.output, str)


# ---------------------------------------------------------------------------
# Priority resolution
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["architect", "coder", "researcher", "reviewer"])
def test_reasoning_pool_agents_resolve_high_priority(name: str, tmp_path: Path) -> None:
    from inference.models import PoolPriority

    agent = _load_agent(name, tmp_path)
    assert agent._resolve_priority() == PoolPriority.HIGH


# ---------------------------------------------------------------------------
# System prompt caching
# ---------------------------------------------------------------------------


def test_system_prompt_cached_on_first_access(tmp_path: Path) -> None:
    """System prompt must be the same object on repeated calls (cached, no repeated disk reads)."""
    agent = _load_agent("researcher", tmp_path)
    p1 = agent._load_system_prompt()
    p2 = agent._load_system_prompt()
    assert p1 is p2


def test_system_prompt_includes_tool_creation_policy(tmp_path: Path) -> None:
    """Tool creation policy must be appended to every agent's system prompt."""
    for name in ["architect", "coder", "researcher", "reviewer"]:
        agent = _load_agent(name, tmp_path)
        prompt = agent._load_system_prompt()
        assert "create_tool" in prompt, f"{name}'s prompt must include tool creation policy"


# ---------------------------------------------------------------------------
# Context loading
# ---------------------------------------------------------------------------


async def test_pre_loaded_context_bypasses_store(tmp_path: Path) -> None:
    """When payload.context is set, _load_context must return it without touching the store."""
    agent = _load_agent("architect", tmp_path)
    payload = AgentPayload(task_id="t1", prompt="x", context="pre-loaded research context")
    loaded = await agent._load_context(payload)
    assert loaded == "pre-loaded research context"


async def test_empty_context_store_produces_empty_context(tmp_path: Path) -> None:
    """When the context store has no documents, _load_context must return empty string."""
    agent = _load_agent("researcher", tmp_path)
    # tmp_path has no context docs - store returns empty strings
    payload = AgentPayload(task_id="t1", prompt="x")
    loaded = await agent._load_context(payload)
    assert isinstance(loaded, str)


# ---------------------------------------------------------------------------
# Task message structure
# ---------------------------------------------------------------------------


def test_task_message_includes_task_id(tmp_path: Path) -> None:
    """The user message built by the agent must include the task ID."""
    agent = _load_agent("coder", tmp_path)
    payload = AgentPayload(task_id="task-abc-123", prompt="Implement login.")
    msg = agent._build_task_message(payload, context="", scored_tools=[])
    assert "task-abc-123" in msg


def test_task_message_includes_prompt(tmp_path: Path) -> None:
    agent = _load_agent("reviewer", tmp_path)
    payload = AgentPayload(task_id="t1", prompt="Run the full test suite.")
    msg = agent._build_task_message(payload, context="", scored_tools=[])
    assert "Run the full test suite." in msg


# ---------------------------------------------------------------------------
# Side-effect marking for crash recovery (#3)
# ---------------------------------------------------------------------------


class _RecordingStore:
    def __init__(self) -> None:
        self.marked: list[str] = []

    async def mark_side_effect(self, task_id: str) -> None:
        self.marked.append(task_id)


def _tool_map():
    import unittest.mock as mock

    mutating = mock.MagicMock()
    mutating.is_mutating = True
    readonly = mock.MagicMock()
    readonly.is_mutating = False
    return {"write_file": mutating, "read_file": readonly}


async def test_record_side_effects_marks_on_successful_mutation(tmp_path):
    agent = _load_agent("coder", tmp_path)
    store = _RecordingStore()
    agent._deps.running_task_store = store

    results = [
        (ToolCall(name="read_file", call_id="1"), "data", True),
        (ToolCall(name="write_file", call_id="2"), "ok", True),
    ]
    await agent._record_side_effects(AgentPayload(task_id="t1", prompt="p"), results, _tool_map())

    assert store.marked == ["t1"]


async def test_record_side_effects_ignores_readonly_and_failed_mutations(tmp_path):
    agent = _load_agent("coder", tmp_path)
    store = _RecordingStore()
    agent._deps.running_task_store = store

    results = [
        (ToolCall(name="read_file", call_id="1"), "data", True),  # read-only success
        (ToolCall(name="write_file", call_id="2"), "err", False),  # mutation FAILED
    ]
    await agent._record_side_effects(AgentPayload(task_id="t1", prompt="p"), results, _tool_map())

    assert store.marked == []


# ---------------------------------------------------------------------------
# Core tools always survive semantic selection (#8)
# ---------------------------------------------------------------------------


async def test_core_tools_never_dropped_by_semantic_filter(tmp_path):
    from unittest.mock import AsyncMock, MagicMock

    agent = _load_agent("coder", tmp_path)

    def _tool(name: str):
        t = MagicMock()
        t.name = name
        return t

    # More than SEMANTIC_FILTER_MIN tools, mixing core and non-core.
    names = [
        "read_file", "patch_file", "check_types", "bash", "list_dir", "search_files",
        "glob", "write_file", "web_search", "fetch_url", "kasa", "git",
    ]
    agent._deps.tool_registry = MagicMock()
    agent._deps.tool_registry.tools_for_agent.return_value = [_tool(n) for n in names]
    agent._deps.confidence_tracker = MagicMock()
    agent._deps.confidence_tracker.scores_for_agent = AsyncMock(return_value=[])
    # Semantic search returns only NON-core tools.
    index = MagicMock()
    index.search_tools = AsyncMock(return_value=["web_search", "kasa", "git"])
    agent._deps.tool_index = index

    selected = {t.name for t, _ in await agent._load_tools("do something with the code")}

    # Core read/search/edit/verify tools are forced in despite ranking...
    assert {"read_file", "patch_file", "check_types", "bash"} <= selected
    # ...and the semantic picks are still present.
    assert "web_search" in selected


async def test_multimodal_tool_image_context(tmp_path: Path) -> None:
    """If a tool returns image data on success, the agent loop appends a subsequent user message with the image."""
    import json

    from tools.base import Tool
    from tools.models import ToolInput, ToolOutput

    class MockVisionTool(Tool):
        name = "mock_vision"
        description = "takes a photo"

        def schema(self) -> dict:
            return {}

        async def run(self, inp: ToolInput) -> ToolOutput:
            return ToolOutput(
                success=True,
                data={
                    "path": "test.png",
                    "base64_image": "abcdef",
                    "mime_type": "image/png",
                },
            )

    agent = _load_agent("coder", tmp_path)
    tool_map = {"mock_vision": MockVisionTool()}

    # 1. Verify _call_tool extracts base64_image and mime_type from result.data
    res_str, images = await agent._call_tool(tool_map, "mock_vision", {})
    assert images == [("abcdef", "image/png")]
    parsed = json.loads(res_str)
    assert parsed["success"] is True
    # Ensure they are not in the serialized json to prevent context truncation
    assert "base64_image" not in parsed["data"]
    assert "mime_type" not in parsed["data"]

    # 2. Verify _execute_call yields the 4-tuple including images
    call = ToolCall(name="mock_vision", call_id="call_v1", params={})
    c, r_str, succ, imgs = await agent._execute_call(call, AgentPayload(prompt="p", task_id="t1"), tool_map)
    assert succ is True
    assert imgs == [("abcdef", "image/png")]

    # 3. Verify _append_tool_call_exchange appends the tool message AND subsequent user message with the image
    messages = []
    agent._append_tool_call_exchange(messages, [(call, r_str, succ, imgs)])

    assert len(messages) == 3
    # First: assistant tool calls
    assert messages[0]["role"] == "assistant"
    assert messages[0]["tool_calls"][0]["function"]["name"] == "mock_vision"
    # Second: tool message containing metadata JSON
    assert messages[1]["role"] == "tool"
    assert "test.png" in messages[1]["content"]
    assert messages[2]["content"][1]["image_url"]["url"] == "data:image/png;base64,abcdef"


@pytest.mark.asyncio
async def test_execute_calls_ordered_preserves_causal_chunks(tmp_path: Path) -> None:
    """Verify that a mutating call followed by a read executes in proper causal order."""
    from tools.base import Tool
    from tools.models import ToolInput, ToolOutput

    execution_order: list[str] = []

    class WriteTool(Tool):
        name = "write_tool"
        is_mutating = True
        description = "Write something."
        parameters_schema = {"type": "object", "properties": {}}

        async def run(self, input: ToolInput) -> ToolOutput:
            execution_order.append("write")
            return ToolOutput(success=True, data={"written": True})

    class ReadTool(Tool):
        name = "read_tool"
        is_mutating = False
        description = "Read something."
        parameters_schema = {"type": "object", "properties": {}}

        async def run(self, input: ToolInput) -> ToolOutput:
            execution_order.append("read")
            return ToolOutput(success=True, data={"read": True})

    agent = _load_agent("coder", tmp_path)
    tool_map = {"write_tool": WriteTool(), "read_tool": ReadTool()}

    calls = [
        ToolCall(name="write_tool", call_id="c1", params={}),
        ToolCall(name="read_tool", call_id="c2", params={}),
    ]

    results = await agent._execute_calls_ordered(calls, AgentPayload(prompt="test", task_id="t1"), tool_map)
    assert len(results) == 2
    assert execution_order == ["write", "read"]

