"""Tests for delegation mechanics in AgenticLLMAgent.

Covers: depth guard, registry-missing failure, empty-task guard,
engineering-agent fallback prevention, depth propagation,
workspace propagation, approval-store missing error,
approval timeout, and approval resolution.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from agents.agentic_llm_agent import AgenticLLMAgent
from agents.constants import MAX_DELEGATION_DEPTH as _MAX_DELEGATION_DEPTH
from agents.general.agent import GeneralAgent
from agents.models import AgentConfig, AgentDependencies, AgentPayload, AgentResult
from context import FileContextStore
from tests.conftest import MockInferenceRouter
from tools.confidence import ConfidenceTracker
from tools.registry import ToolRegistry

AGENTS_DIR = Path(__file__).parent.parent.parent.parent / "agents"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_deps(tmp_path: Path, **overrides) -> AgentDependencies:
    base = AgentDependencies(
        context_store=FileContextStore(tmp_path / "context"),
        inference_router=MockInferenceRouter(),
        tool_registry=ToolRegistry(graph={}, auto_register=False),
        confidence_tracker=ConfidenceTracker(db_path=tmp_path / "tools.db"),
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


def _make_agent(tmp_path: Path, **dep_overrides) -> AgenticLLMAgent:
    config = AgentConfig.from_yaml(AGENTS_DIR / "general" / "config.yaml")
    return GeneralAgent(config, _make_deps(tmp_path, **dep_overrides))


def _payload(depth: int = 0) -> AgentPayload:
    return AgentPayload(task_id="t1", prompt="do something", delegation_depth=depth)


# ---------------------------------------------------------------------------
# Delegation depth guard
# ---------------------------------------------------------------------------


async def test_depth_limit_blocks_delegation(tmp_path: Path) -> None:
    """_delegate_task must return failure JSON when delegation_depth >= limit."""
    agent = _make_agent(tmp_path)
    result_str = await agent._delegate_task(
        _payload(depth=_MAX_DELEGATION_DEPTH),
        {"agent": "general", "task": "sub-task"},
    )
    result = json.loads(result_str)
    assert result["success"] is False
    assert "depth limit" in result["error"].lower()


async def test_one_below_depth_limit_succeeds(tmp_path: Path) -> None:
    """At depth limit - 1, delegation must proceed normally."""
    from agents.registry import AgentRegistry

    deps = _make_deps(tmp_path)
    deps.agent_registry = AgentRegistry(agents_dir=AGENTS_DIR, deps=deps)
    config = AgentConfig.from_yaml(AGENTS_DIR / "general" / "config.yaml")
    agent = GeneralAgent(config, deps)

    result_str = await agent._delegate_task(
        _payload(depth=_MAX_DELEGATION_DEPTH - 1),
        {"agent": "researcher", "task": "research x"},
    )
    result = json.loads(result_str)
    assert result["success"] is True


# ---------------------------------------------------------------------------
# Missing registry
# ---------------------------------------------------------------------------


async def test_delegation_without_registry_returns_failure(tmp_path: Path) -> None:
    """_delegate_task must fail gracefully when no agent_registry is wired."""
    agent = _make_agent(tmp_path)
    assert agent._deps.agent_registry is None

    result_str = await agent._delegate_task(
        _payload(),
        {"agent": "coder", "task": "implement y"},
    )
    result = json.loads(result_str)
    assert result["success"] is False
    assert "registry" in result["error"].lower()


# ---------------------------------------------------------------------------
# Empty task parameter
# ---------------------------------------------------------------------------


async def test_empty_task_param_returns_failure(tmp_path: Path) -> None:
    """delegate_task with an empty 'task' string must return a descriptive failure."""
    from agents.registry import AgentRegistry

    deps = _make_deps(tmp_path)
    deps.agent_registry = AgentRegistry(agents_dir=AGENTS_DIR, deps=deps)
    config = AgentConfig.from_yaml(AGENTS_DIR / "general" / "config.yaml")
    agent = GeneralAgent(config, deps)

    result_str = await agent._delegate_task(
        _payload(),
        {"agent": "researcher", "task": ""},
    )
    result = json.loads(result_str)
    assert result["success"] is False
    assert "task" in result["error"].lower()


# ---------------------------------------------------------------------------
# Engineering agent fallback prevention
# ---------------------------------------------------------------------------


async def test_missing_engineering_agent_does_not_fall_back_to_general(
    tmp_path: Path,
) -> None:
    """When an engineering agent is missing, must NOT silently fall back to 'general'."""
    import unittest.mock as mock

    from agents.registry import AgentRegistry

    deps = _make_deps(tmp_path)
    registry = AgentRegistry(agents_dir=AGENTS_DIR, deps=deps)
    original_get = registry.get

    def _patched_get(name: str):
        if name == "coder":
            raise KeyError("coder not registered")
        return original_get(name)

    with mock.patch.object(registry, "get", side_effect=_patched_get):
        deps.agent_registry = registry
        config = AgentConfig.from_yaml(AGENTS_DIR / "general" / "config.yaml")
        agent = GeneralAgent(config, deps)

        result_str = await agent._delegate_task(
            _payload(),
            {"agent": "coder", "task": "implement something"},
        )

    result = json.loads(result_str)
    assert result["success"] is False
    error = result["error"].lower()
    # Must mention the engineering agent and refuse the fallback
    assert "coder" in error or "engineering agent" in error


# ---------------------------------------------------------------------------
# Sub-payload correctness: depth and workspace propagation
# ---------------------------------------------------------------------------


async def test_delegation_increments_depth(tmp_path: Path) -> None:
    """The sub-payload must have delegation_depth == parent_depth + 1."""
    captured: list[AgentPayload] = []

    class CapturingRegistry:
        def get(self, name: str):
            class CapAgent:
                async def run(self, payload: AgentPayload) -> AgentResult:
                    captured.append(payload)
                    return AgentResult(output="ok", summary="ok")

            return CapAgent()

    deps = _make_deps(tmp_path)
    deps.agent_registry = CapturingRegistry()
    config = AgentConfig.from_yaml(AGENTS_DIR / "general" / "config.yaml")
    agent = GeneralAgent(config, deps)

    parent_depth = 3
    await agent._delegate_task(
        AgentPayload(task_id="t1", prompt="x", delegation_depth=parent_depth),
        {"agent": "researcher", "task": "do research"},
    )
    assert len(captured) == 1
    assert captured[0].delegation_depth == parent_depth + 1


async def test_delegation_propagates_workspace(tmp_path: Path) -> None:
    """The sub-payload must carry the same workspace as the parent payload."""
    captured: list[AgentPayload] = []

    class CapturingRegistry:
        def get(self, name: str):
            class CapAgent:
                async def run(self, payload: AgentPayload) -> AgentResult:
                    captured.append(payload)
                    return AgentResult(output="ok", summary="ok")

            return CapAgent()

    deps = _make_deps(tmp_path)
    deps.agent_registry = CapturingRegistry()
    config = AgentConfig.from_yaml(AGENTS_DIR / "general" / "config.yaml")
    agent = GeneralAgent(config, deps)

    workspace = "/home/user/project"
    await agent._delegate_task(
        AgentPayload(task_id="t1", prompt="x", workspace=workspace),
        {"agent": "architect", "task": "design spec"},
    )
    assert captured[0].workspace == workspace


async def test_delegation_propagates_task_id(tmp_path: Path) -> None:
    """The sub-payload must carry the same task_id as the parent payload."""
    captured: list[AgentPayload] = []

    class CapturingRegistry:
        def get(self, name: str):
            class CapAgent:
                async def run(self, payload: AgentPayload) -> AgentResult:
                    captured.append(payload)
                    return AgentResult(output="ok", summary="ok")

            return CapAgent()

    deps = _make_deps(tmp_path)
    deps.agent_registry = CapturingRegistry()
    config = AgentConfig.from_yaml(AGENTS_DIR / "general" / "config.yaml")
    agent = GeneralAgent(config, deps)

    await agent._delegate_task(
        AgentPayload(task_id="task-xyz-789", prompt="x"),
        {"agent": "tester", "task": "run QA"},
    )
    assert captured[0].task_id == "task-xyz-789"


async def test_delegation_success_returns_sub_agent_output(tmp_path: Path) -> None:
    """On success, _delegate_task must return the sub-agent's output in result JSON."""

    class FixedOutputRegistry:
        def get(self, name: str):
            class FixedAgent:
                async def run(self, payload: AgentPayload) -> AgentResult:
                    return AgentResult(
                        output="Spec written to spec.md.",
                        summary="Spec done",
                    )

            return FixedAgent()

    deps = _make_deps(tmp_path)
    deps.agent_registry = FixedOutputRegistry()
    config = AgentConfig.from_yaml(AGENTS_DIR / "general" / "config.yaml")
    agent = GeneralAgent(config, deps)

    result_str = await agent._delegate_task(
        _payload(),
        {"agent": "architect", "task": "design the feature"},
    )
    result = json.loads(result_str)
    assert result["success"] is True
    assert result["output"] == "Spec written to spec.md."
    assert result["summary"] == "Spec done"


# ---------------------------------------------------------------------------
# Approval flow
# ---------------------------------------------------------------------------


async def test_request_approval_without_store_raises(tmp_path: Path) -> None:
    """request_approval must raise RuntimeError when approval_store is not injected."""
    agent = _make_agent(tmp_path)
    assert agent._deps.approval_store is None

    with pytest.raises(RuntimeError, match="ApprovalStore"):
        await agent._request_approval(
            _payload(),
            {"message": "Delete production database?"},
        )


async def test_request_approval_timeout_returns_timeout_rejected(tmp_path: Path) -> None:
    """When approval times out, _request_approval must return 'timeout_rejected'."""
    from approval.store import ApprovalStore

    agent = _make_agent(tmp_path, approval_store=ApprovalStore(), approval_timeout_seconds=0.01)
    decision = await agent._request_approval(
        _payload(),
        {"message": "Should I proceed?"},
    )
    assert decision == "timeout_rejected"


async def test_request_approval_resolved_approve_returns_status(tmp_path: Path) -> None:
    """When a card is approved, _request_approval must return the resolution status."""
    from approval.store import ApprovalStore

    store = ApprovalStore()
    agent = _make_agent(tmp_path, approval_store=store, approval_timeout_seconds=5.0)

    async def _approve_after_delay():
        await asyncio.sleep(0.05)
        cards = store.pending()
        if cards:
            store.resolve(cards[0].id, "Approve")

    approval_task = asyncio.create_task(_approve_after_delay())
    decision = await agent._request_approval(
        _payload(),
        {"message": "Send the email?", "options": ["Approve", "Reject"]},
    )
    await approval_task
    assert decision == "Approve"


async def test_request_approval_resolved_reject_returns_status(tmp_path: Path) -> None:
    """When a card is rejected, _request_approval must return 'Reject'."""
    from approval.store import ApprovalStore

    store = ApprovalStore()
    agent = _make_agent(tmp_path, approval_store=store, approval_timeout_seconds=5.0)

    async def _reject_after_delay():
        await asyncio.sleep(0.05)
        cards = store.pending()
        if cards:
            store.resolve(cards[0].id, "Reject")

    rejection_task = asyncio.create_task(_reject_after_delay())
    decision = await agent._request_approval(
        _payload(),
        {"message": "Delete old logs?"},
    )
    await rejection_task
    assert decision == "Reject"


# ---------------------------------------------------------------------------
# Tool execution routing
# ---------------------------------------------------------------------------


async def test_unknown_tool_returns_error_json(tmp_path: Path) -> None:
    """_call_tool must return structured error JSON for a tool not in tool_map."""
    agent = _make_agent(tmp_path)
    result_str = await agent._call_tool({}, "nonexistent", {})
    result = json.loads(result_str)
    assert result["success"] is False
    assert "nonexistent" in result["error"]


async def test_tool_exception_returns_error_json(tmp_path: Path) -> None:
    """_call_tool must catch exceptions from tool.run() and return error JSON."""
    from tools.base import Tool
    from tools.models import ToolInput, ToolOutput

    class ExplodingTool(Tool):
        name = "exploding"
        description = "always raises"

        def schema(self) -> dict:
            return {}

        async def run(self, inp: ToolInput) -> ToolOutput:
            raise RuntimeError("Kaboom!")

    agent = _make_agent(tmp_path)
    result_str = await agent._call_tool({"exploding": ExplodingTool()}, "exploding", {})
    result = json.loads(result_str)
    assert result["success"] is False
    assert "Kaboom" in result["error"]
