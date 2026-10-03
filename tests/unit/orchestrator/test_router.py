"""Unit tests for ExecutionPlanner / Router.

See docs/CODING_STYLE.md Sections 5.3, 6.5, 9.7, 13.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from inference import CompletionResponse
from orchestrator.models import ExecutionMode, ExecutionPlan, IntentClassification
from orchestrator.router import ExecutionPlanner, _execution_profile, _requires_consequential_guard


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
    assert "<north_runtime_context>" in call_arg.prompt
    assert call_arg.prompt.index("<north_runtime_context>") < call_arg.prompt.index("=== User Task ===")


# ---------------------------------------------------------------------------
# Engineering tasks: one agent, the general agent, which delegates the coding
# ---------------------------------------------------------------------------


def _engineering_planner(*names: str) -> ExecutionPlanner:
    def _agent(name: str) -> MagicMock:
        a = MagicMock()
        a.name = name
        a.domain = "general"
        a.config.accepts = ""
        return a

    agents = [_agent(n) for n in (names or ("general",))]
    reg = MagicMock()
    reg.all.return_value = agents
    reg.names.return_value = [a.name for a in agents]
    reg.for_domain.side_effect = lambda domain: [a for a in agents if a.domain == domain]
    return ExecutionPlanner(agent_registry=reg, inference_router=MagicMock(), tool_registry=None)


@pytest.mark.parametrize(
    "kind", ["question", "research", "design", "bugfix", "debug", "test", "refactor", "feature", "deploy", "ship"]
)
def test_every_engineering_kind_is_one_general_agent_and_keeps_its_label(kind: str) -> None:
    plan = _engineering_planner()._build_engineering_plan(kind, "t1")

    assert plan.agents == ["general"]
    assert plan.parallel_groups == [["general"]] and plan.dependencies == {}
    assert plan.mode is ExecutionMode.SINGLE_AGENT
    assert plan.engineering_kind == kind


def test_north_has_no_agents_of_its_own_for_coding_any_more() -> None:
    plan = _engineering_planner("general", "coder", "reviewer", "architect", "researcher")._build_engineering_plan(
        "feature", "t1"
    )

    assert plan.agents == ["general"], "a stray registered agent is never planned in"


def test_without_a_general_agent_the_task_falls_back_instead_of_naming_a_missing_agent() -> None:
    planner = _engineering_planner("wellness")

    plan = planner._build_engineering_plan("bugfix", "t1")

    assert "general" not in plan.agents


def test_repository_overview_uses_quick_readonly_profile() -> None:
    classification = IntentClassification(
        is_consequential=False,
        domain="engineering",
        reasoning="repository overview",
        confidence=0.92,
    )
    plan = ExecutionPlan(
        task_id="t1",
        agents=["general"],
        parallel_groups=[["general"]],
        dependencies={},
        mode=ExecutionMode.SINGLE_AGENT,
        engineering_kind="question",
    )

    assert (
        _execution_profile(
            "Check North's repo and provide context about what this project is.",
            classification,
            plan,
        )
        == "quick_readonly"
    )


@pytest.mark.parametrize(
    "prompt",
    [
        "What does this codebase do?",
        "What is this project for?",
        "Walk me through the repository architecture.",
        "How does this repository work?",
    ],
)
def test_repository_overview_prompt_variants_use_quick_profile(prompt: str) -> None:
    classification = IntentClassification(
        is_consequential=False,
        domain="engineering",
        reasoning="repository overview",
        confidence=0.92,
    )
    plan = ExecutionPlan(
        task_id="t1",
        agents=["general"],
        parallel_groups=[["general"]],
        dependencies={},
        mode=ExecutionMode.SINGLE_AGENT,
        engineering_kind="question",
    )

    assert _execution_profile(prompt, classification, plan) == "quick_readonly"


def test_repository_action_question_does_not_look_like_an_overview() -> None:
    classification = IntentClassification(
        is_consequential=False,
        domain="engineering",
        reasoning="repository operation",
        confidence=0.92,
    )
    plan = ExecutionPlan(
        task_id="t1",
        agents=["general"],
        parallel_groups=[["general"]],
        dependencies={},
        mode=ExecutionMode.SINGLE_AGENT,
        engineering_kind="question",
    )

    assert _execution_profile("How do I clone this repository?", classification, plan) == "standard"


@pytest.mark.parametrize(
    "prompt",
    [
        "Use my existing browser over CDP and inspect my logged-in LinkedIn session.",
        "Fill and submit job applications for matching roles.",
        "Create a North flow for this recurring task.",
    ],
)
def test_sensitive_browser_external_actions_and_capabilities_are_consequential(prompt: str) -> None:
    assert _requires_consequential_guard(prompt)


def test_plain_read_only_browser_research_is_not_forced_consequential() -> None:
    assert not _requires_consequential_guard("Open the public documentation in an isolated browser and summarize it.")


@pytest.mark.parametrize(
    "prompt",
    [
        "Audit this repository for security issues.",
        "Give me an overview of this repo and then implement the missing feature.",
        "Investigate this project's performance regression.",
    ],
)
def test_deep_or_mutating_repository_requests_keep_standard_profile(prompt: str) -> None:
    classification = IntentClassification(
        is_consequential=False,
        domain="engineering",
        reasoning="engineering task",
        confidence=0.95,
    )
    plan = ExecutionPlan(
        task_id="t1",
        agents=["general"],
        parallel_groups=[["general"]],
        dependencies={},
        mode=ExecutionMode.SINGLE_AGENT,
        engineering_kind="research",
    )

    assert _execution_profile(prompt, classification, plan) == "standard"


def test_low_confidence_or_multi_agent_overview_keeps_standard_profile() -> None:
    low_confidence = IntentClassification(
        is_consequential=False,
        domain="engineering",
        reasoning="uncertain",
        confidence=0.6,
    )
    multi_agent = ExecutionPlan(
        task_id="t1",
        agents=["researcher", "architect"],
        parallel_groups=[["researcher"], ["architect"]],
        dependencies={"architect": ["researcher"]},
        mode=ExecutionMode.HIERARCHICAL,
        engineering_kind="design",
    )

    assert _execution_profile("Give me an overview of this repository.", low_confidence, multi_agent) == "standard"


def test_confidence_never_changes_the_shape_of_an_engineering_plan() -> None:
    for kind in ("question", "bugfix", "feature", ""):
        for confidence_free in (0.95, 0.4, 0.05):
            plan = _engineering_planner()._build_engineering_plan(kind, "t1")
            assert plan.agents == ["general"], f"{kind!r} @ {confidence_free}"


@pytest.mark.asyncio
async def test_plan_all_overrides_llm_agent_graph_for_engineering() -> None:
    """For engineering, the fixed shape wins over whatever agents the LLM returns."""

    def _agent(name: str) -> MagicMock:
        a = MagicMock()
        a.name = name
        a.domain = "general"
        a.config.accepts = ""
        return a

    reg = MagicMock()
    reg.all.return_value = [_agent("general"), _agent("wellness")]
    reg.names.return_value = ["general", "wellness"]

    inference = MagicMock()
    resp = MagicMock(spec=CompletionResponse)
    # LLM returns a bogus agent graph; only the domain and engineering_kind should matter.
    resp.text = (
        '{"confidence": 0.9, "is_consequential": false, "domain": "engineering",'
        ' "engineering_kind": "bugfix", "reasoning": "x", "mode": "hierarchical",'
        ' "agents": ["wellness", "general"]}'
    )
    inference.complete = AsyncMock(return_value=resp)

    planner = ExecutionPlanner(agent_registry=reg, inference_router=inference, tool_registry=None)
    _, plan = await planner.plan_all(prompt="fix the off-by-one in parser.py", task_id="t1")

    assert plan.agents == ["general"]  # fixed here, not chosen by the LLM
    assert plan.mode is ExecutionMode.SINGLE_AGENT and plan.engineering_kind == "bugfix"


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
