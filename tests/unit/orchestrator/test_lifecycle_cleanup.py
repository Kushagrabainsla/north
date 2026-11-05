"""Tests for task lifecycle teardown across PlanStore, FailureHandler, and CostTracker."""

from __future__ import annotations

from orchestrator.failure_handler import FailureHandler
from orchestrator.plan_store import PlanStore


def test_failure_handler_clear_all():
    handler = FailureHandler(task_context_store=None)
    handler.increment_retry_count("task_1", "agent_a")
    handler.increment_retry_count("task_1", "agent_b")
    handler.increment_retry_count("task_2", "agent_a")

    handler.clear_all("task_1")

    assert handler.increment_retry_count("task_1", "agent_a") == 1, "counted from the start again"
    assert handler.increment_retry_count("task_1", "agent_b") == 1
    assert handler.increment_retry_count("task_2", "agent_a") == 2, "another task's count is kept"


def test_plan_store_clear_lifecycle():
    store = PlanStore()
    store.set_plan("task_1", [{"content": "Step 1", "status": "done"}])
    assert store.progress("task_1") == (1, 1)

    store.clear("task_1")
    assert store.progress("task_1") == (0, 0)
