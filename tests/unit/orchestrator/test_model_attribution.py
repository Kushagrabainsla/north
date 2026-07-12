"""Tests for fix 2a: the orchestrator records an agent's model in the ledger.

The `agent_completed` ledger entry must carry model_used derived from the
agent's models_used, so the audit trail (and later the Definition-of-Done gate)
can see which model produced each result.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from agents.models import AgentResult
from approval.store import ApprovalStore
from orchestrator.orchestrator import Orchestrator


def _orchestrator():
    ledger = MagicMock()
    ledger.write = AsyncMock()
    stream_manager = MagicMock()
    stream_manager.emit = AsyncMock()
    task_context_store = MagicMock()
    task_context_store.write = AsyncMock()
    notifier = MagicMock()
    notifier.notify = AsyncMock()
    orch = Orchestrator(
        ledger=ledger,
        agent_registry=MagicMock(),
        north_star_checker=MagicMock(),
        execution_planner=MagicMock(),
        task_context_store=task_context_store,
        failure_handler=MagicMock(),
        notifier=notifier,
        stream_manager=stream_manager,
        approval_store=ApprovalStore(),
    )
    return orch, ledger


def _completed_entry(ledger):
    for call in ledger.write.call_args_list:
        entry = call.args[0]
        if entry.action == "agent_completed":
            return entry
    raise AssertionError("no agent_completed ledger entry was written")


async def test_agent_completed_ledger_records_model_used():
    orch, ledger = _orchestrator()
    agent = MagicMock()
    agent.name = "general"  # non-engineering: skips the evidence gate
    result = AgentResult(
        output="Done.",
        summary="Done.",
        successful_tools=[],
        models_used=["anthropic/claude-sonnet"],
    )

    await orch._handle_agent_result("t1", agent, result, payload=None)

    entry = _completed_entry(ledger)
    assert entry.model_used == "anthropic/claude-sonnet"


async def test_multiple_models_are_joined():
    orch, ledger = _orchestrator()
    agent = MagicMock()
    agent.name = "general"
    result = AgentResult(
        output="Done.",
        summary="Done.",
        successful_tools=[],
        models_used=["model-a", "model-b"],
    )

    await orch._handle_agent_result("t1", agent, result, payload=None)

    assert _completed_entry(ledger).model_used == "model-a, model-b"


async def test_no_models_used_records_none():
    orch, ledger = _orchestrator()
    agent = MagicMock()
    agent.name = "general"
    result = AgentResult(output="Done.", summary="Done.", successful_tools=[], models_used=[])

    await orch._handle_agent_result("t1", agent, result, payload=None)

    assert _completed_entry(ledger).model_used is None
