"""Tests for Intent-Class Routing Policy (Fast / Deep Paths)."""

from __future__ import annotations

from orchestrator.models import ExecutionMode, ExecutionPath, ExecutionPlan, IntentClassification


def test_intent_classification_execution_path_default():
    ic = IntentClassification(
        is_consequential=False,
        domain="general",
        reasoning="simple question",
    )
    assert ic.execution_path == ExecutionPath.FAST


def test_execution_plan_fast_vs_deep_path():
    fast_plan = ExecutionPlan(
        task_id="t1",
        agents=["general"],
        parallel_groups=[["general"]],
        dependencies={},
        mode=ExecutionMode.SINGLE_AGENT,
        execution_path=ExecutionPath.FAST,
    )
    assert fast_plan.execution_path == ExecutionPath.FAST

    deep_plan = ExecutionPlan(
        task_id="t2",
        agents=["coder", "reviewer"],
        parallel_groups=[["coder"], ["reviewer"]],
        dependencies={"reviewer": ["coder"]},
        mode=ExecutionMode.HIERARCHICAL,
        execution_path=ExecutionPath.DEEP,
    )
    assert deep_plan.execution_path == ExecutionPath.DEEP
