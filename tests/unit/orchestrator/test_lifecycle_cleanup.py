"""Tests for task lifecycle teardown across PlanStore, FailureHandler, and CostTracker."""

from __future__ import annotations

from orchestrator.failure_handler import FailureHandler
from orchestrator.plan_store import PlanStore


def test_failure_handler_clear_all():
    handler = FailureHandler(ledger_writer=None, task_context_store=None)
    handler.increment_retry_count("task_1", "agent_a")
    handler.increment_retry_count("task_1", "agent_b")
    handler.increment_retry_count("task_2", "agent_a")

    assert handler.get_retry_count("task_1", "agent_a") == 1
    assert handler.get_retry_count("task_1", "agent_b") == 1
    assert handler.get_retry_count("task_2", "agent_a") == 1

    handler.clear_all("task_1")

    assert handler.get_retry_count("task_1", "agent_a") == 0
    assert handler.get_retry_count("task_1", "agent_b") == 0
    assert handler.get_retry_count("task_2", "agent_a") == 1


def test_plan_store_clear_lifecycle():
    store = PlanStore()
    store.set_plan("task_1", [{"content": "Step 1", "status": "done"}])
    assert len(store.get_plan("task_1")) == 1

    store.clear("task_1")
    assert len(store.get_plan("task_1")) == 0
