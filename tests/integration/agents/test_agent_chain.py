"""Integration tests for engineering agent delegation chains.

These tests exercise real AgentRegistry + real agent instantiation with a
mock inference router that returns scripted responses per-agent.  They verify
that the delegate_task tool actually wires agents together end-to-end.

Design notes:
- result.output of agent.run() is always the OUTERMOST agent's final LLM call,
  not the innermost sub-agent's output.  Sub-agent outputs appear as tool results
  in the conversation, so the outer agent can reference them in its final answer.
- ChainRouter.call_counts tracks how many times each agent's complete_with_tools
  was called, which lets us verify the delegation chain executed.
- Capturing registries intercept sub-agent run() calls only, not the root call.

No real network calls are made.  No filesystem tools are exercised.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

from agents.models import AgentDependencies, AgentPayload, AgentResult
from agents.registry import AgentRegistry
from approval.store import ApprovalStore
from context import FileContextStore
from inference.models import ToolCall, ToolCallResponse
from tests.conftest import MockInferenceRouter
from tools.confidence import ConfidenceTracker
from tools.registry import ToolRegistry

AGENTS_DIR = Path(__file__).parent.parent.parent.parent / "agents"


# ---------------------------------------------------------------------------
# Agent-aware mock inference router
# ---------------------------------------------------------------------------


class ChainRouter(MockInferenceRouter):
    """Returns scripted ToolCallResponse sequences per agent component name.

    Responses are consumed in FIFO order per agent.  When the queue is
    exhausted, a default final-message response is returned so the loop
    terminates cleanly.
    """

    def __init__(self, responses: dict[str, list[ToolCallResponse]]) -> None:
        self._queues: dict[str, list[ToolCallResponse]] = {k: list(v) for k, v in responses.items()}
        self.call_counts: dict[str, int] = {}

    async def complete_with_tools(
        self,
        request,
        token_callback: Callable[[str], Awaitable[None]] | None = None,
    ) -> ToolCallResponse:
        agent = request.component
        self.call_counts[agent] = self.call_counts.get(agent, 0) + 1
        queue = self._queues.get(agent, [])
        if queue:
            resp = queue.pop(0)
            if resp.type == "message" and token_callback and resp.content:
                await token_callback(resp.content)
            return resp
        text = f"{agent}: done."
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


def _delegate(agent: str, task: str, call_id: str = "d1") -> ToolCallResponse:
    return ToolCallResponse(
        type="tool_calls",
        calls=[ToolCall(name="delegate_task", call_id=call_id, params={"agent": agent, "task": task})],
        model_used="mock",
        tokens_in=10,
        tokens_out=5,
    )


def _msg(text: str) -> ToolCallResponse:
    return ToolCallResponse(
        type="message",
        content=text,
        calls=[],
        model_used="mock",
        tokens_in=10,
        tokens_out=5,
    )


def _make_registry(tmp_path: Path, router: ChainRouter) -> tuple[AgentRegistry, AgentDependencies]:
    deps = AgentDependencies(
        context_store=FileContextStore(tmp_path / "context"),
        inference_router=router,
        tool_registry=ToolRegistry(graph={}, auto_register=False),
        confidence_tracker=ConfidenceTracker(db_path=tmp_path / "tools.db"),
        approval_store=ApprovalStore(),
        agent_max_iterations=10,  # keep tests fast
    )
    registry = AgentRegistry(agents_dir=AGENTS_DIR, deps=deps)
    deps.agent_registry = registry
    return registry, deps


# ---------------------------------------------------------------------------
# Full chain: researcher → architect → coder → tester
# ---------------------------------------------------------------------------


async def test_full_chain_all_four_agents_called(tmp_path: Path) -> None:
    """Starting from researcher, all four agents must be invoked in sequence."""
    task_id = "chain-001"
    # Each agent: first call delegates, second call (after sub-agent completes) produces final answer.
    router = ChainRouter(
        {
            "researcher": [
                _delegate("architect", f"Research done. Task ID: {task_id}.", "d1"),
                _msg("Research and implementation complete."),
            ],
            "architect": [
                _delegate("coder", f"Spec ready. Task ID: {task_id}.", "d2"),
                _msg("Architecture complete."),
            ],
            "coder": [
                _delegate("tester", f"Code done. Task ID: {task_id}.", "d3"),
                _msg("Implementation complete."),
            ],
            "tester": [
                _msg("All tests pass. QA complete."),
            ],
        }
    )
    registry, _ = _make_registry(tmp_path, router)

    result = await registry.get("researcher").run(AgentPayload(task_id=task_id, prompt="Build feature X."))

    for name in ("researcher", "architect", "coder", "tester"):
        assert router.call_counts.get(name, 0) >= 1, f"{name} was not called"
    assert isinstance(result.output, str)
    assert result.output  # non-empty


async def test_full_chain_result_is_valid_agentresult(tmp_path: Path) -> None:
    """researcher.run() must return a valid AgentResult after the full chain completes."""
    task_id = "chain-002"
    router = ChainRouter(
        {
            "researcher": [
                _delegate("architect", "Research done.", "d1"),
                _msg("Chain complete. Feature delivered."),
            ],
            "architect": [_delegate("coder", "Spec ready.", "d2"), _msg("ok")],
            "coder": [_delegate("tester", "Code done.", "d3"), _msg("ok")],
            "tester": [_msg("PASS - 42 tests passed.")],
        }
    )
    registry, _ = _make_registry(tmp_path, router)

    result = await registry.get("researcher").run(AgentPayload(task_id=task_id, prompt="Build feature Y."))
    assert result.cost_usd >= 0.0
    assert not result.requires_approval
    assert "Chain complete" in result.output


# ---------------------------------------------------------------------------
# Tester → coder fix cycle
# ---------------------------------------------------------------------------


async def test_tester_delegates_to_coder_on_code_bug(tmp_path: Path) -> None:
    """Tester must route to coder when it classifies failures as code bugs."""
    task_id = "fix-001"
    # Tester call 1: delegates to coder.
    # Coder call 1: delegates back to tester.
    # Sub-tester call 1: returns final pass message.
    # Coder call 2 (after sub-tester): final message.
    # Outer tester call 2 (after coder): final message.
    router = ChainRouter(
        {
            "tester": [
                _delegate("coder", f"QA failed - code bug. Task ID: {task_id}. Fix test_x.", "d1"),
            ],
            "coder": [
                _delegate("tester", f"Fix applied. Task ID: {task_id}. Re-run QA.", "d2"),
            ],
        }
    )
    registry, _ = _make_registry(tmp_path, router)

    await registry.get("tester").run(AgentPayload(task_id=task_id, prompt=f"Run QA for {task_id}."))

    assert router.call_counts.get("coder", 0) >= 1, "coder must be called during fix cycle"
    assert router.call_counts.get("tester", 0) >= 2, "tester must run at least twice (initial + re-run)"


async def test_tester_fix_cycle_terminates_with_pass(tmp_path: Path) -> None:
    """After coder fixes the bugs, tester must reach a final answer."""
    task_id = "fix-002"
    # Sub-tester gets the queued responses in order after outer tester consumes its first.
    # Outer tester call 1: delegate to coder.
    # Sub-tester call 1 (re-run after fix): "PASS" message.
    # Outer tester call 2: default done message.
    # Queue order for "tester":
    #   [0] outer tester call 1 → delegates to coder
    #   [1] sub-tester call 1   → final pass message (consumed by the inner tester instance)
    #   [2] outer tester call 2 → final summary after delegation returns
    router = ChainRouter(
        {
            "tester": [
                _delegate("coder", "QA failed. Fix.", "d1"),
                _msg("PASS - sub-run all tests pass."),
                _msg("PASS - all tests pass after fix."),
            ],
            "coder": [
                _delegate("tester", "Fixed. Re-run.", "d2"),
                _msg("coder done."),
            ],
        }
    )
    registry, _ = _make_registry(tmp_path, router)

    result = await registry.get("tester").run(AgentPayload(task_id=task_id, prompt=f"Run QA for {task_id}."))
    assert "PASS" in result.output


# ---------------------------------------------------------------------------
# Tester → architect spec gap cycle
# ---------------------------------------------------------------------------


async def test_tester_delegates_to_architect_on_spec_gap(tmp_path: Path) -> None:
    """Tester must route to architect (not coder) when it finds a spec gap."""
    task_id = "spec-001"
    router = ChainRouter(
        {
            "tester": [
                _delegate("architect", f"Spec gap found. Task ID: {task_id}. Update spec.", "d1"),
            ],
        }
    )
    registry, _ = _make_registry(tmp_path, router)

    await registry.get("tester").run(AgentPayload(task_id=task_id, prompt=f"Run QA for {task_id}."))

    assert router.call_counts.get("architect", 0) >= 1
    assert "coder" not in router.call_counts, "spec gaps must go to architect, not coder"


# ---------------------------------------------------------------------------
# No spurious delegation
# ---------------------------------------------------------------------------


async def test_researcher_does_not_delegate_when_llm_returns_message(tmp_path: Path) -> None:
    """When LLM returns a final message (no tool call), researcher must not delegate."""
    router = ChainRouter({})  # all agents return default final-message
    registry, _ = _make_registry(tmp_path, router)

    await registry.get("researcher").run(AgentPayload(task_id="r1", prompt="Research existing auth patterns."))

    assert router.call_counts.get("researcher", 0) == 1
    for name in ("architect", "coder", "tester"):
        assert name not in router.call_counts, f"{name} must not be called"


async def test_architect_does_not_delegate_when_llm_returns_message(tmp_path: Path) -> None:
    """When LLM returns a final message, architect must not chain to coder."""
    router = ChainRouter({})
    registry, _ = _make_registry(tmp_path, router)

    await registry.get("architect").run(AgentPayload(task_id="a1", prompt="Design the caching architecture."))

    assert router.call_counts.get("architect", 0) == 1
    for name in ("coder", "tester"):
        assert name not in router.call_counts, f"{name} must not be called"


# ---------------------------------------------------------------------------
# Delegation depth propagation
# ---------------------------------------------------------------------------


async def test_delegation_depth_increments_at_each_hop(tmp_path: Path) -> None:
    """Each sub-agent invoked via delegate_task must have delegation_depth incremented by 1."""
    observed_depths: dict[str, int] = {}

    class DepthCapturingRegistry:
        def __init__(self, real: AgentRegistry) -> None:
            self._real = real

        def get(self, name: str):
            real_agent = self._real.get(name)
            _orig_run = real_agent.run

            async def _run(payload: AgentPayload) -> AgentResult:
                observed_depths[name] = payload.delegation_depth
                return await _orig_run(payload)

            real_agent.run = _run
            return real_agent

    router = ChainRouter(
        {
            "researcher": [_delegate("architect", "Research done.", "d1")],
            "architect": [_delegate("coder", "Spec ready.", "d2")],
            "coder": [_delegate("tester", "Code done.", "d3")],
        }
    )
    registry, deps = _make_registry(tmp_path, router)
    deps.agent_registry = DepthCapturingRegistry(registry)

    # Root researcher is called directly (not via capturing registry).
    # Sub-agents (architect, coder, tester) go through DepthCapturingRegistry.
    await registry.get("researcher").run(
        AgentPayload(task_id="depth-test", prompt="Build feature.", delegation_depth=0)
    )

    # Sub-agents are captured; each should be one level deeper than its parent.
    assert observed_depths.get("architect", -1) == 1, "architect must be at depth 1"
    assert observed_depths.get("coder", -1) == 2, "coder must be at depth 2"
    assert observed_depths.get("tester", -1) == 3, "tester must be at depth 3"


# ---------------------------------------------------------------------------
# Workspace propagation through chain
# ---------------------------------------------------------------------------


async def test_workspace_propagated_to_sub_agents(tmp_path: Path) -> None:
    """Workspace from the root payload must be passed to all sub-agents."""
    observed: dict[str, str] = {}
    workspace = str(tmp_path / "user_project")

    class WorkspaceCapture:
        def __init__(self, real: AgentRegistry) -> None:
            self._real = real

        def get(self, name: str):
            real_agent = self._real.get(name)
            _orig = real_agent.run

            async def _run(p: AgentPayload) -> AgentResult:
                observed[name] = p.workspace
                return await _orig(p)

            real_agent.run = _run
            return real_agent

    router = ChainRouter(
        {
            "researcher": [_delegate("architect", "Research done.", "d1")],
            "architect": [_delegate("coder", "Spec ready.", "d2")],
            "coder": [_delegate("tester", "Code done.", "d3")],
        }
    )
    registry, deps = _make_registry(tmp_path, router)
    deps.agent_registry = WorkspaceCapture(registry)

    await registry.get("researcher").run(AgentPayload(task_id="ws-test", prompt="Build feature.", workspace=workspace))

    # Sub-agents must all receive the workspace from the root payload.
    for name in ("architect", "coder", "tester"):
        assert observed.get(name) == workspace, f"{name} must receive workspace='{workspace}'"


# ---------------------------------------------------------------------------
# Task ID propagation
# ---------------------------------------------------------------------------


async def test_task_id_consistent_through_sub_agents(tmp_path: Path) -> None:
    """All sub-agents in the chain must share the same task_id as the root payload."""
    task_id = "consistent-task-id-123"
    observed: dict[str, str] = {}

    class TaskIdCapture:
        def __init__(self, real: AgentRegistry) -> None:
            self._real = real

        def get(self, name: str):
            real_agent = self._real.get(name)
            _orig = real_agent.run

            async def _run(p: AgentPayload) -> AgentResult:
                observed[name] = p.task_id
                return await _orig(p)

            real_agent.run = _run
            return real_agent

    router = ChainRouter(
        {
            "researcher": [_delegate("architect", "Research done.", "d1")],
            "architect": [_delegate("coder", "Spec ready.", "d2")],
            "coder": [_delegate("tester", "Code done.", "d3")],
        }
    )
    registry, deps = _make_registry(tmp_path, router)
    deps.agent_registry = TaskIdCapture(registry)

    await registry.get("researcher").run(AgentPayload(task_id=task_id, prompt="Build feature."))

    for name in ("architect", "coder", "tester"):
        assert observed.get(name) == task_id, f"{name} must use task_id='{task_id}'"


# ---------------------------------------------------------------------------
# Depth limit terminates runaway delegation
# ---------------------------------------------------------------------------


async def test_delegation_depth_limit_stops_infinite_chain(tmp_path: Path) -> None:
    """A runaway delegation chain must terminate when _MAX_DELEGATION_DEPTH is hit."""
    # All agents permanently try to delegate - would loop forever without the cap.
    router = ChainRouter(
        {
            "researcher": [_delegate("architect", "Keep going.", f"r{i}") for i in range(15)],
            "architect": [_delegate("coder", "Keep going.", f"a{i}") for i in range(15)],
            "coder": [_delegate("tester", "Keep going.", f"c{i}") for i in range(15)],
            "tester": [_delegate("coder", "Keep going.", f"t{i}") for i in range(15)],
        }
    )
    registry, deps = _make_registry(tmp_path, router)
    # Tight iteration cap to prevent each agent from spending 40 iters trying to redelegate
    deps.agent_max_iterations = 5

    result = await registry.get("researcher").run(
        AgentPayload(task_id="depth-limit", prompt="Build feature.", delegation_depth=0)
    )

    # Must terminate and return a string (not hang or recurse infinitely)
    assert isinstance(result.output, str)
    assert result.output
