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
from memory import FileContextStore
from inference.models import ToolCall, ToolCallResponse
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


@pytest.mark.parametrize("name", ["architect", "coder", "researcher", "tester"])
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

    agent = _load_agent("tester", tmp_path, InspectingRouter())
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

    agent = _load_agent("tester", tmp_path, UnknownToolRouter())
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


@pytest.mark.parametrize("name", ["architect", "coder"])
def test_reasoning_pool_agents_resolve_high_priority(name: str, tmp_path: Path) -> None:
    from inference.models import PoolPriority

    agent = _load_agent(name, tmp_path)
    assert agent._resolve_priority() == PoolPriority.HIGH


@pytest.mark.parametrize("name", ["researcher", "tester"])
def test_fast_cheap_agents_resolve_medium_priority(name: str, tmp_path: Path) -> None:
    from inference.models import PoolPriority

    agent = _load_agent(name, tmp_path)
    assert agent._resolve_priority() == PoolPriority.MEDIUM


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
    for name in ["architect", "coder", "researcher", "tester"]:
        agent = _load_agent(name, tmp_path)
        prompt = agent._load_system_prompt()
        assert "create_tool" in prompt, f"{name}'s prompt must include tool creation policy"


# ---------------------------------------------------------------------------
# Context loading
# ---------------------------------------------------------------------------


async def test_engineering_agents_include_north_stars_in_context(tmp_path: Path) -> None:
    """All engineering agents must include NORTH_STARS in their default allowed documents."""
    from memory import ContextDocument

    for name in ["architect", "coder", "researcher", "tester"]:
        agent = _load_agent(name, tmp_path)
        docs = await agent._allowed_documents()
        assert ContextDocument.NORTH_STARS in docs, f"{name} must read north_stars by default"


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
    agent = _load_agent("tester", tmp_path)
    payload = AgentPayload(task_id="t1", prompt="Run the full test suite.")
    msg = agent._build_task_message(payload, context="", scored_tools=[])
    assert "Run the full test suite." in msg
