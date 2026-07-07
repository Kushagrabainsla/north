"""Unit tests for ExecutionPlanner / Router.

See docs/CODING_STYLE.md Sections 5.3, 6.5, 9.7, 13.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from inference import CompletionResponse
from orchestrator.router import ExecutionPlanner


@pytest.mark.asyncio
async def test_execution_planner_workspace_context_in_prompt() -> None:
    """ExecutionPlanner must inject the workspace and absolute path instruction

    into the planner prompt if a workspace path is configured.
    """
    mock_agent = MagicMock()
    mock_agent.name = "general"
    mock_agent.domain = "general"
    mock_agent.config.accepts = "text"

    mock_agent_registry = MagicMock()
    mock_agent_registry.all.return_value = [mock_agent]
    mock_agent_registry.names.return_value = ["general"]

    mock_inference = MagicMock()
    mock_response = MagicMock(spec=CompletionResponse)
    mock_response.text = (
        '{"confidence": 0.9, "is_consequential": false, "domain": "general",'
        ' "reasoning": "test", "mode": "single_agent", "agents": ["general"]}'
    )
    mock_inference.complete = AsyncMock(return_value=mock_response)

    planner = ExecutionPlanner(
        agent_registry=mock_agent_registry,
        inference_router=mock_inference,
        tool_registry=None,
        workspace="/path/to/my/workspace",
    )

    classification, plan = await planner.plan_all(
        prompt="List directory contents",
        task_id="t1",
    )

    # Verify that mock_inference.complete was called
    mock_inference.complete.assert_called_once()
    call_arg = mock_inference.complete.call_args[0][0]

    # Verify the workspace instruction was injected in the prompt
    assert "=== System Context ===" in call_arg.prompt
    assert "- workspace (default cwd for shell/file tools): /path/to/my/workspace" in call_arg.prompt
    assert "always prefer absolute paths" in call_arg.prompt


# ---------------------------------------------------------------------------
# Deterministic engineering pipeline (#4)
# ---------------------------------------------------------------------------


def _engineering_planner() -> ExecutionPlanner:
    from orchestrator.models import ExecutionMode  # noqa: F401

    def _agent(name: str) -> MagicMock:
        a = MagicMock()
        a.name = name
        a.domain = "engineering"
        a.config.accepts = ""
        return a

    reg = MagicMock()
    reg.all.return_value = [_agent(n) for n in ("researcher", "architect", "coder", "reviewer")]
    return ExecutionPlanner(agent_registry=reg, inference_router=MagicMock(), tool_registry=None)


def test_feature_runs_full_chain_in_order() -> None:
    from orchestrator.models import ExecutionMode

    plan = _engineering_planner()._build_engineering_plan("feature", 0.9, "t1")
    assert plan.agents == ["researcher", "architect", "coder", "reviewer"]
    assert plan.mode is ExecutionMode.HIERARCHICAL
    assert plan.dependencies == {"architect": ["researcher"], "coder": ["architect"], "reviewer": ["coder"]}
    # Sequential stages, one agent each.
    assert plan.parallel_groups == [["researcher"], ["architect"], ["coder"], ["reviewer"]]


def test_bugfix_runs_coder_then_reviewer() -> None:
    plan = _engineering_planner()._build_engineering_plan("bugfix", 0.9, "t1")
    assert plan.agents == ["coder", "reviewer"]
    assert plan.dependencies == {"reviewer": ["coder"]}


def test_refactor_runs_architect_coder_reviewer() -> None:
    plan = _engineering_planner()._build_engineering_plan("refactor", 0.9, "t1")
    assert plan.agents == ["architect", "coder", "reviewer"]


def test_research_runs_researcher_then_architect() -> None:
    plan = _engineering_planner()._build_engineering_plan("research", 0.9, "t1")
    assert plan.agents == ["researcher", "architect"]


def test_question_is_single_researcher() -> None:
    from orchestrator.models import ExecutionMode

    plan = _engineering_planner()._build_engineering_plan("question", 0.9, "t1")
    assert plan.agents == ["researcher"]
    assert plan.mode is ExecutionMode.SINGLE_AGENT


def test_low_confidence_forces_full_chain() -> None:
    # bugfix would be coder→reviewer, but low confidence runs the full chain.
    plan = _engineering_planner()._build_engineering_plan("bugfix", 0.4, "t1")
    assert plan.agents == ["researcher", "architect", "coder", "reviewer"]


def test_unknown_kind_defaults_to_full_chain() -> None:
    plan = _engineering_planner()._build_engineering_plan("", 0.9, "t1")
    assert plan.agents == ["researcher", "architect", "coder", "reviewer"]


def test_coder_is_always_followed_by_reviewer() -> None:
    for kind in ("bugfix", "refactor", "feature"):
        plan = _engineering_planner()._build_engineering_plan(kind, 0.9, "t1")
        assert "reviewer" in plan.agents
        assert plan.agents.index("coder") < plan.agents.index("reviewer")


@pytest.mark.asyncio
async def test_plan_all_overrides_llm_agent_graph_for_engineering() -> None:
    """For engineering, the deterministic table wins over whatever agents the LLM returns."""
    from orchestrator.models import ExecutionMode

    def _agent(name: str) -> MagicMock:
        a = MagicMock()
        a.name = name
        a.domain = "engineering"
        a.config.accepts = ""
        return a

    reg = MagicMock()
    reg.all.return_value = [_agent(n) for n in ("researcher", "architect", "coder", "reviewer")]
    reg.names.return_value = ["researcher", "architect", "coder", "reviewer"]

    inference = MagicMock()
    resp = MagicMock(spec=CompletionResponse)
    # LLM returns a bogus agent graph; only engineering_kind should matter.
    resp.text = (
        '{"confidence": 0.9, "is_consequential": false, "domain": "engineering",'
        ' "engineering_kind": "bugfix", "reasoning": "x", "mode": "hierarchical",'
        ' "agents": ["architect", "researcher"]}'
    )
    inference.complete = AsyncMock(return_value=resp)

    planner = ExecutionPlanner(agent_registry=reg, inference_router=inference, tool_registry=None)
    _, plan = await planner.plan_all(prompt="fix the off-by-one in parser.py", task_id="t1")

    assert plan.agents == ["coder", "reviewer"]  # from the table, not the LLM
    assert plan.mode is ExecutionMode.HIERARCHICAL


@pytest.mark.asyncio
async def test_planner_fails_honestly_after_retries(monkeypatch) -> None:
    """A persistent planner LLM failure raises (task fails) - never a silent no-op."""
    from orchestrator import router as router_mod
    from orchestrator.exceptions import RoutingError

    monkeypatch.setattr(router_mod, "_PLANNER_RETRY_DELAY_S", 0)  # no sleeping in tests

    reg = MagicMock()
    agent = MagicMock()
    agent.name = "general"
    agent.domain = "general"
    agent.config.accepts = ""
    reg.all.return_value = [agent]
    reg.names.return_value = ["general"]

    inference = MagicMock()
    inference.complete = AsyncMock(side_effect=RuntimeError("all models rate limited"))

    planner = ExecutionPlanner(agent_registry=reg, inference_router=inference, tool_registry=None)
    with pytest.raises(RoutingError):
        await planner.plan_all(prompt="do a thing", task_id="t1")
    # retried the configured number of times before giving up
    assert inference.complete.await_count == router_mod._PLANNER_MAX_ATTEMPTS


@pytest.mark.asyncio
async def test_planner_recovers_on_retry(monkeypatch) -> None:
    """A transient failure followed by a good response still yields a plan."""
    from orchestrator import router as router_mod

    monkeypatch.setattr(router_mod, "_PLANNER_RETRY_DELAY_S", 0)

    reg = MagicMock()
    agent = MagicMock()
    agent.name = "general"
    agent.domain = "general"
    agent.config.accepts = ""
    reg.all.return_value = [agent]
    reg.names.return_value = ["general"]

    good = MagicMock(spec=CompletionResponse)
    good.text = (
        '{"confidence": 0.9, "is_consequential": false, "domain": "general",'
        ' "reasoning": "ok", "mode": "single_agent", "agents": ["general"]}'
    )
    inference = MagicMock()
    inference.complete = AsyncMock(side_effect=[RuntimeError("transient"), good])

    planner = ExecutionPlanner(agent_registry=reg, inference_router=inference, tool_registry=None)
    classification, plan = await planner.plan_all(prompt="list files", task_id="t1")
    assert classification.domain == "general"
    assert inference.complete.await_count == 2
