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


def test_debug_and_test_route_through_coder_reviewer() -> None:
    # debug and test are conductor task-framings: coder→reviewer, and the plan
    # carries the kind so the conductor and DoD can specialise.
    for kind in ("debug", "test"):
        plan = _engineering_planner()._build_engineering_plan(kind, 0.9, "t1")
        assert plan.agents == ["coder", "reviewer"], kind
        assert plan.engineering_kind == kind


def test_deploy_is_single_coder_no_reviewer() -> None:
    # Deploy/ship is a single git/gh-capable agent - no reviewer, and never escalated
    # to the full chain even at low confidence (there is no new code to review).
    for kind in ("deploy", "ship"):
        plan = _engineering_planner()._build_engineering_plan(kind, 0.9, "t1")
        assert plan.agents == ["coder"], kind
        assert plan.engineering_kind == kind
    assert _engineering_planner()._build_engineering_plan("deploy", 0.2, "t1").agents == ["coder"]


def test_refactor_runs_architect_coder_reviewer() -> None:
    plan = _engineering_planner()._build_engineering_plan("refactor", 0.9, "t1")
    assert plan.agents == ["architect", "coder", "reviewer"]


def test_research_ends_with_the_agent_that_found_the_answer() -> None:
    """Investigation returns findings, so there is nothing for a design stage to do.

    Pairing the two meant every "investigate X and summarise it" ran an architect
    with nothing to design - 21s, no spec written - and made the run multi-agent,
    which pulled in a synthesis pass on top.
    """
    plan = _engineering_planner()._build_engineering_plan("research", 0.9, "t1")
    assert plan.agents == ["researcher"]


def test_design_is_where_the_architect_belongs() -> None:
    """A task that actually wants an approach decided still gets one."""
    plan = _engineering_planner()._build_engineering_plan("design", 0.9, "t1")
    assert plan.agents == ["researcher", "architect"]


def test_question_is_single_researcher() -> None:
    from orchestrator.models import ExecutionMode

    plan = _engineering_planner()._build_engineering_plan("question", 0.9, "t1")
    assert plan.agents == ["researcher"]
    assert plan.mode is ExecutionMode.SINGLE_AGENT


def test_low_confidence_forces_full_chain() -> None:
    # bugfix would be coder→reviewer, but low confidence broadens a *code* task
    # to the full chain (a read-only kind is NOT broadened this way - see below).
    plan = _engineering_planner()._build_engineering_plan("bugfix", 0.4, "t1")
    assert plan.agents == ["researcher", "architect", "coder", "reviewer"]


def test_low_confidence_no_code_kinds_never_add_coder() -> None:
    # Regression: a low-confidence read-only kind must stay read-only. A vague
    # "how does X work?" (question) or "investigate Y" (research) must never be
    # escalated into a write task by adding the coder.
    assert _engineering_planner()._build_engineering_plan("question", 0.4, "t1").agents == ["researcher"]
    assert _engineering_planner()._build_engineering_plan("research", 0.4, "t1").agents == ["researcher"]
    assert _engineering_planner()._build_engineering_plan("design", 0.4, "t1").agents == [
        "researcher",
        "architect",
    ]


def test_no_code_kinds_are_read_only_at_every_confidence() -> None:
    for kind in ("question", "research", "design"):
        for confidence in (0.95, 0.6, 0.59, 0.4, 0.05):
            plan = _engineering_planner()._build_engineering_plan(kind, confidence, "t1")
            assert "coder" not in plan.agents, f"{kind}@{confidence} leaked coder"
            assert "reviewer" not in plan.agents, f"{kind}@{confidence} leaked reviewer"


def test_unknown_kind_defaults_to_full_chain() -> None:
    plan = _engineering_planner()._build_engineering_plan("", 0.9, "t1")
    assert plan.agents == ["researcher", "architect", "coder", "reviewer"]


def test_coder_is_always_followed_by_reviewer() -> None:
    for kind in ("bugfix", "debug", "test", "refactor", "feature"):
        plan = _engineering_planner()._build_engineering_plan(kind, 0.9, "t1")
        assert "coder" in plan.agents, kind
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


def test_normalize_plan_json_unwraps_list_responses() -> None:
    """The planner LLM sometimes emits a list instead of an object; _normalize_plan_json
    must unwrap the common shapes so downstream .get() calls don't crash.

    This is the exact failure behind 'planner failed after 3 attempts: list object
    has no attribute get' in the ledger.
    """
    from orchestrator.router import _normalize_plan_json

    # Plain object passthrough.
    obj = {"agents": ["general"], "mode": "single_agent"}
    assert _normalize_plan_json(obj) is obj

    # Single-element list wrapping the object we wanted.
    assert _normalize_plan_json([obj]) == obj

    # List of candidates: pick the first plan-like dict.
    assert _normalize_plan_json([{"foo": 1}, {"agents": ["home"], "mode": "x"}]) == {
        "agents": ["home"],
        "mode": "x",
    }

    # Bare list of agent-name strings -> agent list.
    assert _normalize_plan_json(["researcher", "coder"]) == {"agents": ["researcher", "coder"]}


@pytest.mark.asyncio
async def test_planner_handles_list_json_response(monkeypatch) -> None:
    """A planner response wrapped in a JSON list must still produce a plan, not the
    historic ''list' object has no attribute 'get'' crash.
    """
    from orchestrator import router as router_mod

    monkeypatch.setattr(router_mod, "_PLANNER_RETRY_DELAY_S", 0)

    reg = MagicMock()
    agent = MagicMock()
    agent.name = "general"
    agent.domain = "general"
    agent.config.accepts = ""
    reg.all.return_value = [agent]
    reg.names.return_value = ["general"]

    # Model returns a single-element list instead of an object.
    good = MagicMock(spec=CompletionResponse)
    good.text = (
        '[{"confidence": 0.9, "is_consequential": false, "domain": "general",'
        ' "reasoning": "ok", "mode": "single_agent", "agents": ["general"]}]'
    )
    inference = MagicMock()
    inference.complete = AsyncMock(return_value=good)

    planner = ExecutionPlanner(agent_registry=reg, inference_router=inference, tool_registry=None)
    classification, plan = await planner.plan_all(prompt="list files", task_id="t1")
    assert classification.domain == "general"
    assert plan.agents == ["general"]
