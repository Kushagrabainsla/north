"""Tests for AgenticLLMAgent ReAct loop mechanics.

Each engineering agent is a thin subclass of AgenticLLMAgent - all domain-
specific behaviour lives in system prompts.  These tests verify the loop
itself: final answer path, tool execution, cost accumulation, iteration cap,
unknown-tool resilience, priority resolution, and context loading.

No real network calls are made; inference is fully mocked.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.agentic_llm_agent import MAX_UNANSWERED_APPROVALS
from agents.models import AgentConfig, AgentDependencies, AgentPayload
from inference.models import ToolCall, ToolCallResponse
from memory import FileContextStore
from tests.conftest import MockInferenceRouter
from tools.base import Tool
from tools.confidence import ConfidenceTracker
from tools.models import ToolInput, ToolOutput
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
        tool_registry=ToolRegistry(auto_register=False),
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


async def test_each_model_turn_emits_prompt_section_attribution(tmp_path: Path) -> None:
    class RecordingStream:
        def __init__(self) -> None:
            self.events: list[tuple[str, dict]] = []

        async def emit(self, task_id: str, event: str, data: dict) -> None:
            self.events.append((event, data))

    class UsageRouter(MockInferenceRouter):
        async def complete_with_tools(self, request, token_callback=None):
            return ToolCallResponse(
                type="message",
                content="done",
                calls=[],
                model_used="mock",
                tokens_in=123,
                tokens_out=7,
                cached_tokens=40,
            )

    stream = RecordingStream()
    agent = _load_agent("researcher", tmp_path, UsageRouter())
    agent.deps.stream_manager = stream
    payload = AgentPayload(task_id="profile-task", prompt="inspect", context="earlier turn")

    await agent.run(payload)

    profiles = [data for event, data in stream.events if event == "prompt_profile"]
    assert len(profiles) == 1
    assert profiles[0]["actual_input_tokens"] == 123
    assert profiles[0]["cached_tokens"] == 40
    assert profiles[0]["sections"]["caller_context"] > 0
    assert profiles[0]["sections"]["system_instructions"] > 0
    assert profiles[0]["sections"]["tool_schemas"] > 0
    assert profiles[0]["estimated_input_tokens"] == sum(profiles[0]["sections"].values())


async def test_quick_profile_soft_budget_allows_needed_work_to_continue(tmp_path: Path) -> None:
    class EvidenceTool(Tool):
        name = "gather_evidence"
        description = "Gather another piece of read-only evidence."
        parameters_schema = {"type": "object", "properties": {}}

        async def run(self, input: ToolInput) -> ToolOutput:
            return ToolOutput(success=True, data={"evidence": "ok"})

    class RecordingStream:
        def __init__(self) -> None:
            self.events: list[tuple[str, dict]] = []

        async def emit(self, task_id: str, event: str, data: dict) -> None:
            self.events.append((event, data))

    class SlowEvidenceRouter(MockInferenceRouter):
        def __init__(self) -> None:
            self.requests = []

        async def complete_with_tools(self, request, token_callback=None):
            self.requests.append(request)
            if len(self.requests) <= 3:
                return ToolCallResponse(
                    type="tool_calls",
                    calls=[ToolCall(name="gather_evidence", call_id=f"c{len(self.requests)}", params={})],
                    model_used="mock",
                    tokens_in=10,
                    tokens_out=2,
                )
            return ToolCallResponse(
                type="message",
                content="finished with enough evidence",
                calls=[],
                model_used="mock",
                tokens_in=10,
                tokens_out=5,
            )

    stream = RecordingStream()
    router = SlowEvidenceRouter()
    agent = _load_agent("researcher", tmp_path, router)
    agent.deps.stream_manager = stream
    agent.deps.tool_registry.register(EvidenceTool())

    result = await agent.run(
        AgentPayload(task_id="quick-task", prompt="inspect", execution_profile="quick_readonly")
    )

    assert result.output == "finished with enough evidence"
    assert len(router.requests) == 4
    notices = [data for event, data in stream.events if event == "budget_soft_limit"]
    assert notices == [
        {
            "profile": "quick_readonly",
            "reason": "model_turns",
            "completed_turns": 3,
            "tool_calls": 3,
            "turn_budget": 3,
            "tool_budget": 6,
        }
    ]
    assert any(
        "Soft efficiency budget reached" in (message.get("content") or "")
        for message in router.requests[-1].messages
    )
    assert "Do not create handoff artifacts" in router.requests[0].messages[0]["content"]
    manifests = [data for event, data in stream.events if event == "evidence_manifest"]
    assert manifests == [
        {"profile": "quick_readonly", "references": [{"tool": "gather_evidence"}]},
        {"profile": "quick_readonly", "references": [{"tool": "gather_evidence"}]},
        {"profile": "quick_readonly", "references": [{"tool": "gather_evidence"}]},
    ]


async def test_quick_evidence_manifest_keeps_locators_not_tool_content(tmp_path: Path) -> None:
    class RecordingStream:
        def __init__(self) -> None:
            self.events: list[tuple[str, dict]] = []

        async def emit(self, task_id: str, event: str, data: dict) -> None:
            self.events.append((event, data))

    stream = RecordingStream()
    agent = _load_agent("researcher", tmp_path)
    agent.deps.stream_manager = stream
    payload = AgentPayload(task_id="quick-evidence", prompt="inspect", execution_profile="quick_readonly")
    call = ToolCall(
        name="read_file",
        call_id="read-1",
        params={
            "path": "src/router.py",
            "content": "private fetched content",
            "url": "https://example.com/docs?token=secret",
        },
    )

    await agent._record_quick_evidence(payload, [(call, "private tool output", True, [])], _tool_map())

    manifest = [data for event, data in stream.events if event == "evidence_manifest"][0]
    assert manifest["references"] == [
        {"tool": "read_file", "path": "src/router.py", "url": "https://example.com/docs"}
    ]
    assert "private" not in str(manifest)
    assert "secret" not in str(manifest)


async def test_quick_profile_refuses_mutation_before_it_happens(tmp_path: Path) -> None:
    class MutatingTool(Tool):
        name = "write_artifact"
        description = "Write a bulky artifact."
        is_mutating = True

        def __init__(self) -> None:
            self.called = False

        async def run(self, input: ToolInput) -> ToolOutput:
            self.called = True
            return ToolOutput(success=True)

    agent = _load_agent("researcher", tmp_path)
    tool = MutatingTool()
    payload = AgentPayload(task_id="quick-mutation", prompt="inspect", execution_profile="quick_readonly")

    _call, result, success, _images = await agent._safe_execute_call(
        ToolCall(name=tool.name, call_id="write-1", params={}),
        payload,
        {tool.name: tool},
    )

    assert success is False
    assert "unavailable in the quick read-only profile" in result
    assert tool.called is False


async def test_quick_profile_escalates_after_tool_failure(tmp_path: Path) -> None:
    class RecordingStream:
        def __init__(self) -> None:
            self.events: list[tuple[str, dict]] = []

        async def emit(self, task_id: str, event: str, data: dict) -> None:
            self.events.append((event, data))

    class FailureThenAnswerRouter(MockInferenceRouter):
        def __init__(self) -> None:
            self.calls = 0

        async def complete_with_tools(self, request, token_callback=None):
            self.calls += 1
            if self.calls == 1:
                return ToolCallResponse(
                    type="tool_calls",
                    calls=[ToolCall(name="missing_tool", call_id="missing", params={})],
                    model_used="mock",
                )
            return ToolCallResponse(type="message", content="recovered", calls=[], model_used="mock")

    stream = RecordingStream()
    agent = _load_agent("researcher", tmp_path, FailureThenAnswerRouter())
    agent.deps.stream_manager = stream
    payload = AgentPayload(task_id="quick-failure", prompt="inspect", execution_profile="quick_readonly")

    result = await agent.run(payload)

    assert result.output == "recovered"
    assert payload.execution_profile == "standard"
    escalations = [data for event, data in stream.events if event == "execution_profile_escalated"]
    assert escalations == [{"from": "quick_readonly", "to": "standard", "reason": "tool_failure"}]


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


async def test_context_loading_records_content_free_section_boundaries(tmp_path: Path) -> None:
    agent = _load_agent("researcher", tmp_path)
    payload = AgentPayload(task_id="t1", prompt="x", context="previous conversation")

    await agent._load_context(payload, selected_skills=[])

    assert payload.context_sections == {"caller_context": "previous conversation"}


async def test_engineering_context_includes_repository_evidence_identity(tmp_path: Path, monkeypatch) -> None:
    class Identity:
        def render(self) -> str:
            return "## Repository evidence identity\n- Git commit: `abc123`"

    monkeypatch.setattr("agents.base.repository_identity", lambda workspace: Identity())
    agent = _load_agent("researcher", tmp_path)
    payload = AgentPayload(task_id="repo-revision", prompt="inspect", workspace=str(tmp_path))

    loaded = await agent._load_context(payload, selected_skills=[])

    assert "Git commit: `abc123`" in loaded
    assert payload.context_sections["repository_revision"] in loaded


async def test_context_loading_emits_content_free_memory_telemetry(tmp_path: Path) -> None:
    from memory import MemoryContext, MemoryPrincipal

    class Memory:
        async def principal_for(self, name, domain):
            return MemoryPrincipal(
                name=name,
                domain=domain,
                allowed_domains=frozenset({"engineering", "general"}),
                allowed_fact_topics=frozenset({"preferences", "user"}),
            )

        async def recall(self, principal, prompt):
            return MemoryContext(facts=["the user has a private preference"], episodes=["a private prior task"])

    class RecordingStream:
        def __init__(self) -> None:
            self.events: list[tuple[str, dict]] = []

        async def emit(self, task_id: str, event: str, data: dict) -> None:
            self.events.append((event, data))

    agent = _load_agent("researcher", tmp_path)
    stream = RecordingStream()
    agent.deps.memory = Memory()
    agent.deps.stream_manager = stream

    await agent._load_context(AgentPayload(task_id="memory-profile", prompt="inspect"), selected_skills=[])

    telemetry = [data for event, data in stream.events if event == "memory_recalled"][0]
    assert telemetry["source_counts"] == {"facts": 1, "episodes": 1, "documents": 0}
    assert telemetry["fact_scope"] == ["preferences", "user"]
    assert telemetry["episode_scope"] == ["engineering", "general"]
    assert "private" not in str(telemetry)


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
# Explicit tool requests always survive semantic selection
# ---------------------------------------------------------------------------


async def test_explicit_tool_name_never_dropped_by_semantic_filter(tmp_path):
    from unittest.mock import AsyncMock, MagicMock

    agent = _load_agent("coder", tmp_path)

    def _tool(name: str):
        t = MagicMock()
        t.name = name
        t.description = f"Perform {name.replace('_', ' ')} operations"
        return t

    # More than SEMANTIC_FILTER_MIN globally eligible tools.
    names = [
        "read_file",
        "patch_file",
        "check_types",
        "bash",
        "list_dir",
        "search_files",
        "glob",
        "write_file",
        "web_search",
        "fetch_url",
        "kasa",
        "git",
    ]
    agent._deps.tool_registry = MagicMock()
    agent._deps.tool_registry.available_tools.return_value = [_tool(n) for n in names]
    agent._deps.confidence_tracker = MagicMock()
    agent._deps.confidence_tracker.scores_for_agent = AsyncMock(return_value=[])
    # Semantic search misses the explicitly requested tool.
    index = MagicMock()
    index.update_tools = AsyncMock(return_value=0)
    index.search_tools = AsyncMock(return_value=["web_search", "kasa", "git"])
    agent._deps.tool_index = index

    selected = {
        t.name
        for t, _ in await agent._load_tools(
            AgentPayload(task_id="tools", prompt="Use read_file before implementing the change")
        )
    }

    assert "read_file" in selected
    assert "web_search" in selected


async def test_task_selection_uses_global_catalog_not_agent_mapping(tmp_path: Path) -> None:
    """Semantic relevance trims schemas without turning the result into an allowlist."""
    agent = _load_agent("general", tmp_path)

    def _tool(name: str):
        t = MagicMock()
        t.name = name
        t.description = f"Perform {name.replace('_', ' ')} operations"
        return t

    names = [
        "web_search",
        "fetch_url",
        "read_file",
        "write_file",
        "patch_file",
        "bash",
        "git",
        "kasa",
        "take_photo",
        "schedule_task",
        "update_plan",
    ]
    agent._deps.tool_registry = MagicMock()
    agent._deps.tool_registry.available_tools.return_value = [_tool(name) for name in names]
    agent._deps.confidence_tracker = MagicMock()
    agent._deps.confidence_tracker.scores_for_agent = AsyncMock(return_value=[])
    index = MagicMock()
    index.update_tools = AsyncMock(return_value=0)
    index.search_tools = AsyncMock(return_value=["web_search"])
    agent._deps.tool_index = index

    loaded = await agent._load_tools(
        AgentPayload(task_id="global-tools", prompt="Use kasa to check the lights and research their model")
    )
    loaded_names = {tool.name for tool, _ in loaded}

    assert loaded_names == {"web_search", "kasa", "update_plan"}
    index.search_tools.assert_awaited_once()
    indexed_profiles = dict(index.update_tools.await_args.args[0])
    assert "Tool name: web search (web_search)." in indexed_profiles["web_search"]


async def test_find_tools_loads_a_missed_global_tool(tmp_path: Path) -> None:
    agent = _load_agent("general", tmp_path)
    photo = _AbsentFileTool()
    photo.name = "take_photo"
    photo.description = "Capture a photograph with a connected camera"
    agent._deps.tool_registry = ToolRegistry(auto_register=False)
    agent._deps.tool_registry.register(photo)
    agent._deps.tool_index = None
    tool_map: dict[str, Tool] = {}

    _, result_json, success, _ = await agent._execute_call(
        ToolCall(name="find_tools", call_id="find-1", params={"query": "capture a photograph"}),
        AgentPayload(task_id="find-tools", prompt="take a picture"),
        tool_map,
    )

    assert success is True
    assert "take_photo" in tool_map
    assert "take_photo" in result_json


async def test_multimodal_tool_image_context(tmp_path: Path) -> None:
    """If a tool returns image data on success, the agent loop appends a subsequent user message with the image."""
    import json

    from tools.base import Tool
    from tools.models import ToolOutput

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
    from tools.models import ToolOutput

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


async def test_append_tool_call_exchange_contiguous_tool_roles_with_visuals(tmp_path: Path) -> None:
    """All role='tool' messages must precede any visual user messages for OpenAI protocol compliance."""
    agent = _load_agent("coder", tmp_path)
    messages: list[dict] = []

    results = [
        (ToolCall(name="take_screenshot", call_id="c1", params={}), "ok", True, [("b64img", "image/png")]),
        (ToolCall(name="read_file", call_id="c2", params={}), "file content", True, []),
    ]

    agent._append_tool_call_exchange(messages, results)

    # Message structure:
    # 0: assistant tool_calls
    # 1: tool c1
    # 2: tool c2
    # 3: user visual context
    assert len(messages) == 4
    assert messages[0]["role"] == "assistant"
    assert len(messages[0]["tool_calls"]) == 2
    assert messages[1]["role"] == "tool"
    assert messages[1]["tool_call_id"] == "c1"
    assert messages[2]["role"] == "tool"
    assert messages[2]["tool_call_id"] == "c2"
    assert messages[3]["role"] == "user"
    assert "image_url" in messages[3]["content"][1]["type"]


async def test_load_tools_no_cap_and_skill_tool_inclusion(tmp_path: Path) -> None:
    """_load_tools must not arbitrarily truncate to 10 and must include tools required by active skills."""
    from unittest.mock import AsyncMock, MagicMock

    from skills.models import Skill

    agent = _load_agent("general", tmp_path)

    def _tool(name: str):
        t = MagicMock()
        t.name = name
        return t

    # Register 15 tools
    names = [
        "read_file",
        "patch_file",
        "check_types",
        "bash",
        "list_dir",
        "search_files",
        "glob",
        "write_file",
        "web_search",
        "fetch_url",
        "kasa",
        "git",
        "take_screenshot",
        "take_photo",
        "custom_tool",
    ]
    agent._deps.tool_registry = MagicMock()
    agent._deps.tool_registry.available_tools.return_value = [_tool(n) for n in names]
    agent._deps.confidence_tracker = MagicMock()
    agent._deps.confidence_tracker.scores_for_agent = AsyncMock(return_value=[])

    # Fake skill that mentions take_screenshot
    fake_skill = Skill(
        name="screen-inspection",
        description="Inspect what is on the screen using take_screenshot",
        body="Use take_screenshot to capture desktop displays.",
        directory=tmp_path,
        domains=frozenset({"general"}),
    )
    skill_registry = MagicMock()
    skill_registry.all.return_value = [fake_skill]
    agent._deps.skill_registry = skill_registry

    skill_selector = MagicMock()
    skill_selector.select = AsyncMock(return_value=[fake_skill])
    agent._deps.skill_selector = skill_selector

    # No semantic tool index wired (fallback mode)
    agent._deps.tool_index = None

    # run() selects once and passes the result in; do the same here.
    loaded = await agent._load_tools(
        AgentPayload(task_id="tools", prompt="check my screen"),
        await agent._select_skills("check my screen"),
    )
    loaded_names = {t.name for t, _ in loaded}

    # All 15 tools are available without a 10-tool cap
    assert len(loaded) == 15
    assert "take_screenshot" in loaded_names
    assert "take_photo" in loaded_names
    assert "custom_tool" in loaded_names


# ---------------------------------------------------------------------------
# Failure kinds - "that isn't there" is not a broken tool
# ---------------------------------------------------------------------------


def _registering(agent, tool):
    """Put *tool* in the agent's registry, available to every agent."""
    agent._deps.tool_registry.register(tool)
    return agent


class _AbsentFileTool(Tool):
    """Answers "not found" correctly - a working tool, not a broken one."""

    name = "read_file"
    description = "read a file"
    parameters_schema = {"type": "object", "properties": {}}

    async def run(self, input: ToolInput) -> ToolOutput:
        return ToolOutput(success=False, error="File not found: /nope", failure_kind="not_found")


class _BrokenTool(Tool):
    name = "read_file"
    description = "read a file"
    parameters_schema = {"type": "object", "properties": {}}

    async def run(self, input: ToolInput) -> ToolOutput:
        return ToolOutput(success=False, error="disk exploded")


def _call_then_finish(tool_name: str):
    class Router(MockInferenceRouter):
        calls = 0

        async def complete_with_tools(self, request, token_callback=None):
            Router.calls += 1
            if Router.calls == 1:
                return ToolCallResponse(
                    type="tool_calls",
                    calls=[ToolCall(name=tool_name, call_id="c1", params={})],
                    model_used="mock",
                    tokens_in=1,
                    tokens_out=1,
                )
            return ToolCallResponse(
                type="message", content="done", calls=[], model_used="mock", tokens_in=1, tokens_out=1
            )

    Router.calls = 0
    return Router()


async def test_not_found_does_not_count_against_the_tool(tmp_path: Path) -> None:
    """`read_file` decayed to the lowest-ranked tool the researcher had, purely
    from being asked whether an optional file existed and answering correctly."""
    agent = _load_agent("researcher", tmp_path, _call_then_finish("read_file"))
    _registering(agent, _AbsentFileTool())
    before = await agent._deps.confidence_tracker.get_score("researcher", "read_file")

    await agent.run(AgentPayload(task_id="t-nf", prompt="Look for a file."))
    await asyncio.sleep(0.05)  # confidence is recorded on a spawned task

    after = await agent._deps.confidence_tracker.get_score("researcher", "read_file")
    assert after == before, "answering 'not found' is not a malfunction"


async def test_a_real_tool_error_still_counts_against_the_tool(tmp_path: Path) -> None:
    agent = _load_agent("researcher", tmp_path, _call_then_finish("read_file"))
    _registering(agent, _BrokenTool())
    before = await agent._deps.confidence_tracker.get_score("researcher", "read_file")

    await agent.run(AgentPayload(task_id="t-err", prompt="Look for a file."))
    await asyncio.sleep(0.05)

    after = await agent._deps.confidence_tracker.get_score("researcher", "read_file")
    assert after < before, "a tool that actually broke must lose confidence"


# ---------------------------------------------------------------------------
# An approval nobody answers must not spin the loop
# ---------------------------------------------------------------------------


class _UnansweredApprovalTool(Tool):
    """Stands in for any gated tool whose approval card expires."""

    name = "gated"
    description = "needs approval"
    parameters_schema = {"type": "object", "properties": {}}

    async def run(self, input: ToolInput) -> ToolOutput:
        return ToolOutput(
            success=False,
            failure_kind="refused",
            data={"unanswered": True},
            error="No one answered the approval request within 300s.",
        )


async def test_repeated_unanswered_approvals_stop_the_run(tmp_path: Path) -> None:
    """Nobody is there: keep asking and every card costs a full timeout, which is
    how one abandoned task kept calling the provider for twelve minutes."""

    class AlwaysCallsGatedTool(MockInferenceRouter):
        calls = 0

        async def complete_with_tools(self, request, token_callback=None):
            AlwaysCallsGatedTool.calls += 1
            return ToolCallResponse(
                type="tool_calls",
                calls=[ToolCall(name="gated", call_id=f"c{AlwaysCallsGatedTool.calls}", params={})],
                model_used="mock",
                tokens_in=1,
                tokens_out=1,
            )

    AlwaysCallsGatedTool.calls = 0
    router = AlwaysCallsGatedTool()
    agent = _load_agent("coder", tmp_path, router)
    agent._deps.agent_max_iterations = 40
    _registering(agent, _UnansweredApprovalTool())

    result = await agent.run(AgentPayload(task_id="t-unans", prompt="Do the gated thing."))

    assert AlwaysCallsGatedTool.calls == MAX_UNANSWERED_APPROVALS, (
        "the run must stop at the cap, not spend the whole iteration budget"
    )
    assert "no answer" in result.output.lower()


async def test_ordinary_work_between_two_expired_cards_does_not_clear_the_count(tmp_path: Path) -> None:
    """Reading a file is not evidence that a human is at the keyboard.

    An earlier version reset on any batch that raised no card. Observed live: a
    coder raised a patch_file card, read some files, raised a git card, and
    sailed straight past the cap while nobody was there to answer either one.
    """

    class GatedThenReadThenGated(MockInferenceRouter):
        calls = 0

        async def complete_with_tools(self, request, token_callback=None):
            GatedThenReadThenGated.calls += 1
            name = "reader" if GatedThenReadThenGated.calls == 2 else "gated"
            return ToolCallResponse(
                type="tool_calls",
                calls=[ToolCall(name=name, call_id=f"c{GatedThenReadThenGated.calls}", params={})],
                model_used="mock",
                tokens_in=1,
                tokens_out=1,
            )

    class _ReaderTool(Tool):
        name = "reader"
        description = "an ordinary successful read"
        parameters_schema = {"type": "object", "properties": {}}

        async def run(self, input: ToolInput) -> ToolOutput:
            return ToolOutput(success=True, data={"ok": True})

    GatedThenReadThenGated.calls = 0
    agent = _load_agent("coder", tmp_path, GatedThenReadThenGated())
    agent._deps.agent_max_iterations = 40
    _registering(agent, _UnansweredApprovalTool())
    _registering(agent, _ReaderTool())

    result = await agent.run(AgentPayload(task_id="t-interleaved", prompt="Do the gated thing."))

    assert GatedThenReadThenGated.calls == 3, "two expired cards end the run, read or no read"
    assert "no answer" in result.output.lower()


# ---------------------------------------------------------------------------
# Prompt-cache prefix stability
#
# A cache is a prefix match, so one changed byte near the front throws the whole
# thing away with no error raised - the only symptom is the reuse count sitting
# at zero (see inference/usage.py). These invariants are what keep the prefix
# stable, and nothing else would notice if they broke.
# ---------------------------------------------------------------------------


class _CapturingRouter(MockInferenceRouter):
    def __init__(self) -> None:
        super().__init__()
        self.tool_names: list[str] = []
        self.system_prompt: str = ""
        self.user_prompt: str = ""

    async def complete_with_tools(self, request, token_callback=None):
        self.tool_names = [t.get("function", {}).get("name", "") for t in (request.tools or [])]
        self.system_prompt = next((m["content"] for m in request.messages if m.get("role") == "system"), "")
        self.user_prompt = next((m["content"] for m in request.messages if m.get("role") == "user"), "")
        return ToolCallResponse(type="message", content="ok", calls=[], model_used="mock", tokens_in=1, tokens_out=1)


async def _capture(tmp_path: Path, scores: list[tuple[str, float]]) -> _CapturingRouter:
    router = _CapturingRouter()
    agent = _load_agent("researcher", tmp_path, router)
    for name in ("alpha_tool", "beta_tool", "gamma_tool"):
        tool = _AbsentFileTool()
        tool.name = name
        agent._deps.tool_registry.register(tool)
    agent._deps.confidence_tracker = MagicMock()
    agent._deps.confidence_tracker.scores_for_agent = AsyncMock(return_value=scores)
    agent._deps.confidence_tracker.record_use = AsyncMock(return_value=0.5)
    await agent.run(AgentPayload(task_id="t-cache", prompt="hello"))
    return router


async def test_tool_order_on_the_wire_ignores_confidence(tmp_path: Path) -> None:
    """Ranking tools by a score that moves on every call rewrote the prefix."""
    high_alpha = await _capture(tmp_path, [("alpha_tool", 0.9), ("beta_tool", 0.2), ("gamma_tool", 0.1)])
    high_gamma = await _capture(tmp_path, [("alpha_tool", 0.1), ("beta_tool", 0.2), ("gamma_tool", 0.9)])

    registry_tools = [n for n in high_alpha.tool_names if n.endswith("_tool")]
    assert registry_tools == sorted(registry_tools), "wire order must be by name"
    assert high_alpha.tool_names == high_gamma.tool_names, (
        "a confidence change must not reorder the tool definitions in the prefix"
    )


async def test_dynamic_clock_is_after_the_cached_prefix(tmp_path: Path) -> None:
    """The system stays stable while fresh runtime metadata sits beside the task."""
    import re as _re

    router = await _capture(tmp_path, [])
    assert router.system_prompt, "expected a system prompt"
    assert not _re.search(r"\d{2}:\d{2}", router.system_prompt), (
        f"system prompt still carries a clock: {router.system_prompt[:200]!r}"
    )
    assert "local_time:" not in router.system_prompt
    assert "trusted factual metadata" in router.system_prompt
    assert "<north_runtime_context>" in router.user_prompt
    assert _re.search(r"local_time: \d{4}-\d{2}-\d{2}T\d{2}:\d{2}[+-]\d{2}:\d{2}", router.user_prompt)
    assert router.user_prompt.index("<north_runtime_context>") < router.user_prompt.index("## Task\nhello")
