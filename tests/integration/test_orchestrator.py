"""Integration tests for the Orchestrator pipeline and related subsystems.

These tests exercise the full classify → north-star → route → execute pipeline
using MockInferenceRouter (no real network calls) and isolated SQLite databases
in pytest's tmp_path.  They guard against regressions in the core orchestration
logic that unit tests on storage primitives cannot catch.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agents.models import AgentDependencies, AgentPayload
from approval.store import ApprovalStore
from approval.terminal import TerminalNotifier
from context import FileContextStore
from context.extraction import ExtractionPipeline
from jobs import SQLiteJobProcessor
from ledger import SQLiteLedgerWriter
from ledger.base import LedgerFilters
from ledger.models import LedgerEntry, LedgerSource, LedgerStatus
from orchestrator.failure_handler import FailureHandler
from orchestrator.models import TaskRequest
from orchestrator.orchestrator import Orchestrator
from orchestrator.stream import EventStreamManager
from orchestrator.task_context import TaskContextStore

# Import shared test utilities from conftest
from tests.conftest import MockInferenceRouter
from tools.confidence import ConfidenceTracker
from tools.registry import ToolRegistry
from utils.db import open_db_connection
from utils.ids import generate_id
from utils.time import utcnow

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_orchestrator(tmp_path: Path) -> tuple[Orchestrator, SQLiteLedgerWriter, ApprovalStore]:
    """Build a minimal Orchestrator wired to tmp_path with mock inference."""
    from agents.registry import AgentRegistry
    from orchestrator.north_star import NorthStarChecker
    from orchestrator.router import ExecutionPlanner
    from orchestrator.synthesizer import ResultSynthesizer

    ledger = SQLiteLedgerWriter(tmp_path / "ledger.db")
    inference = MockInferenceRouter()
    context_store = FileContextStore(tmp_path / "context")
    SQLiteJobProcessor(tmp_path / "jobs.db")
    stream = EventStreamManager()
    approval = ApprovalStore()
    task_ctx = TaskContextStore(db_path=tmp_path / "tasks.db")

    agents_dir = Path(__file__).parent.parent.parent / "agents"
    tool_registry = ToolRegistry(graph={}, auto_register=False)
    confidence_tracker = ConfidenceTracker(db_path=tmp_path / "tools.db")

    agent_deps = AgentDependencies(
        context_store=context_store,
        inference_router=inference,
        tool_registry=tool_registry,
        confidence_tracker=confidence_tracker,
        stream_manager=stream,
        approval_store=approval,
    )
    agent_registry = AgentRegistry(agents_dir=agents_dir, deps=agent_deps)
    agent_deps.agent_registry = agent_registry

    failure_handler = FailureHandler(
        ledger_writer=ledger,
        task_context_store=task_ctx,
        stream_manager=stream,
    )

    orch = Orchestrator(
        ledger=ledger,
        agent_registry=agent_registry,
        north_star_checker=NorthStarChecker(context_store, inference),
        execution_planner=ExecutionPlanner(agent_registry, inference, tool_registry),
        task_context_store=task_ctx,
        failure_handler=failure_handler,
        notifier=TerminalNotifier(),
        stream_manager=stream,
        approval_store=approval,
        synthesizer=ResultSynthesizer(inference_router=inference),
    )
    return orch, ledger, approval


# ---------------------------------------------------------------------------
# Orchestrator pipeline tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_submit_task_returns_task_id(tmp_path):
    """submit_task() must return a non-empty task_id immediately."""
    orch, _, _ = _make_orchestrator(tmp_path)
    response = await orch.submit_task(
        TaskRequest(prompt="What is 2 + 2?", source=LedgerSource.PROMPT)
    )
    assert response.task_id
    assert response.status == LedgerStatus.PENDING.value


@pytest.mark.asyncio
async def test_submit_task_writes_pending_ledger_entry(tmp_path):
    """The initial ledger write must happen synchronously before the background task runs."""
    orch, ledger, _ = _make_orchestrator(tmp_path)
    response = await orch.submit_task(
        TaskRequest(prompt="Hello", source=LedgerSource.PROMPT)
    )
    entries = await ledger.query(LedgerFilters(task_id=response.task_id))
    assert len(entries) >= 1
    assert entries[0].action == "task_received"


@pytest.mark.asyncio
async def test_task_pipeline_completes(tmp_path):
    """After the background coroutine runs, the ledger must contain a completed entry."""
    orch, ledger, _ = _make_orchestrator(tmp_path)
    response = await orch.submit_task(
        TaskRequest(prompt="List something", source=LedgerSource.PROMPT)
    )
    # Give the async pipeline a moment to finish (MockInferenceRouter is instant)
    await asyncio.sleep(0.3)

    entries = await ledger.query(LedgerFilters(task_id=response.task_id))
    actions = [e.action for e in entries]
    assert "task_completed" in actions


@pytest.mark.asyncio
async def test_cancel_task_writes_cancelled_entry(tmp_path):
    """cancel_task() must write a task_cancelled ledger entry."""
    orch, ledger, _ = _make_orchestrator(tmp_path)
    response = await orch.submit_task(
        TaskRequest(prompt="Long running task", source=LedgerSource.PROMPT)
    )
    await orch.cancel_task(response.task_id)

    entries = await ledger.query(LedgerFilters(task_id=response.task_id))
    actions = [e.action for e in entries]
    assert "task_cancelled" in actions


@pytest.mark.asyncio
async def test_concurrent_task_cap_raises(tmp_path):
    """submit_task() must raise OrchestratorError when the concurrent cap is hit."""
    import unittest.mock as mock

    from orchestrator.exceptions import OrchestratorError
    from orchestrator.orchestrator import _MAX_CONCURRENT_TASKS

    orch, _, _ = _make_orchestrator(tmp_path)

    # Fill _active_tasks with fake entries to simulate the cap being hit
    fake_tasks = {f"task_{i}": mock.MagicMock() for i in range(_MAX_CONCURRENT_TASKS)}
    orch._active_tasks = fake_tasks  # type: ignore[assignment]

    with pytest.raises(OrchestratorError, match="Too many concurrent tasks"):
        await orch.submit_task(TaskRequest(prompt="one more", source=LedgerSource.PROMPT))


@pytest.mark.asyncio
async def test_strategy_command_completes_without_agent(tmp_path):
    """'switch to eco mode' must short-circuit before routing to any agent."""
    orch, ledger, _ = _make_orchestrator(tmp_path)
    response = await orch.submit_task(
        TaskRequest(prompt="switch to eco mode", source=LedgerSource.PROMPT)
    )
    await asyncio.sleep(0.1)

    entries = await ledger.query(LedgerFilters(task_id=response.task_id))
    actions = [e.action for e in entries]
    # Strategy commands write agent_completed and then task_completed
    assert "task_completed" in actions or "agent_completed" in actions


# ---------------------------------------------------------------------------
# North star: low-confidence skip
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_north_star_skipped_on_low_confidence(tmp_path):
    """When the planner returns confidence < 0.7, north star check must be skipped."""
    import unittest.mock as mock

    from orchestrator.models import ExecutionMode, ExecutionPlan, IntentClassification
    from orchestrator.orchestrator import _NORTH_STAR_CONFIDENCE_THRESHOLD

    orch, ledger, _ = _make_orchestrator(tmp_path)

    low_conf = IntentClassification(
        is_consequential=True,
        domain="general",
        reasoning="borderline",
        confidence=_NORTH_STAR_CONFIDENCE_THRESHOLD - 0.1,
    )
    dummy_plan = ExecutionPlan(
        task_id="t1",
        agents=["general"],
        parallel_groups=[["general"]],
        dependencies={},
        mode=ExecutionMode.SINGLE_AGENT,
    )

    # Patch plan_all to return our low-confidence classification
    with mock.patch.object(
        orch._execution_planner, "plan_all", return_value=(low_conf, dummy_plan)
    ), mock.patch.object(orch._north_star_checker, "check_alignment") as mock_check:
        await orch.submit_task(
            TaskRequest(prompt="send an email", source=LedgerSource.PROMPT)
        )
        await asyncio.sleep(0.3)
        # check_alignment must NOT have been called
        mock_check.assert_not_called()


# ---------------------------------------------------------------------------
# Task context store
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_task_context_write_and_read(tmp_path):
    """write() followed by read() must return the same value."""
    store = TaskContextStore(db_path=tmp_path / "tasks.db")
    await store.initialize_task("t1", ["agent_a"])
    await store.write("t1", "agent_a", "answer", 42)
    result = await store.read("t1", "agent_a", "answer", timeout=5)
    assert result == 42


@pytest.mark.asyncio
async def test_task_context_cleanup_removes_old_rows(tmp_path):
    """cleanup_stale_tasks() must delete rows older than the retention window."""
    import datetime
    store = TaskContextStore(db_path=tmp_path / "tasks.db")

    # Write a row with an artificially old timestamp
    from utils.db import open_db_connection
    db = tmp_path / "tasks.db"
    await store.initialize_task("old_task", ["agent_x"])

    # Back-date the written_at to 10 days ago
    old_ts = (utcnow() - datetime.timedelta(days=10)).isoformat()
    def _backdate():
        with open_db_connection(db) as conn:
            conn.execute("UPDATE task_state SET written_at = ? WHERE task_id = ?", (old_ts, "old_task"))
            conn.commit()
    await asyncio.to_thread(_backdate)

    removed = await store.cleanup_stale_tasks(active_task_ids=frozenset(), retention_days=7)
    assert removed > 0

    # Verify rows are gone
    def _count():
        with open_db_connection(db) as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM task_state WHERE task_id = 'old_task'"
            ).fetchone()[0]
    count = await asyncio.to_thread(_count)
    assert count == 0


@pytest.mark.asyncio
async def test_task_context_cleanup_skips_active(tmp_path):
    """cleanup_stale_tasks() must not delete rows for active task_ids."""
    import datetime
    store = TaskContextStore(db_path=tmp_path / "tasks.db")
    await store.initialize_task("active_task", ["agent_y"])

    db = tmp_path / "tasks.db"
    old_ts = (utcnow() - datetime.timedelta(days=10)).isoformat()
    def _backdate():
        with open_db_connection(db) as conn:
            conn.execute("UPDATE task_state SET written_at = ? WHERE task_id = ?", (old_ts, "active_task"))
            conn.commit()
    await asyncio.to_thread(_backdate)

    removed = await store.cleanup_stale_tasks(
        active_task_ids=frozenset(["active_task"]), retention_days=7
    )
    assert removed == 0


# ---------------------------------------------------------------------------
# Extraction pipeline
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_extraction_pipeline_runs_without_error(tmp_path):
    """run_once() must not raise even when there are no ledger entries."""
    ledger = SQLiteLedgerWriter(tmp_path / "ledger.db")
    context_store = FileContextStore(tmp_path / "context")
    inference = MockInferenceRouter()

    pipeline = ExtractionPipeline(
        ledger=ledger,
        context_store=context_store,
        inference_router=inference,
        north_home=tmp_path,
    )
    count = await pipeline.run_once()
    assert count == 0


@pytest.mark.asyncio
async def test_extraction_pipeline_skips_system_source(tmp_path):
    """Entries with source=SYSTEM must be skipped without an LLM call."""
    import unittest.mock as mock

    ledger = SQLiteLedgerWriter(tmp_path / "ledger.db")
    context_store = FileContextStore(tmp_path / "context")
    inference = MockInferenceRouter()

    await ledger.write(LedgerEntry(
        id=generate_id(),
        timestamp=utcnow(),
        source=LedgerSource.SYSTEM,
        action="startup",
        status=LedgerStatus.COMPLETED,
    ))

    pipeline = ExtractionPipeline(
        ledger=ledger,
        context_store=context_store,
        inference_router=inference,
        north_home=tmp_path,
    )

    with mock.patch.object(inference, "complete") as mock_complete:
        count = await pipeline.run_once()
        mock_complete.assert_not_called()
    assert count == 0


@pytest.mark.asyncio
async def test_extraction_pipeline_advances_watermark(tmp_path):
    """After processing an entry, the watermark file must be written."""
    ledger = SQLiteLedgerWriter(tmp_path / "ledger.db")
    context_store = FileContextStore(tmp_path / "context")
    inference = MockInferenceRouter()

    await ledger.write(LedgerEntry(
        id=generate_id(),
        timestamp=utcnow(),
        source=LedgerSource.PROMPT,
        input="I prefer window seats",
        action="task_received",
        status=LedgerStatus.COMPLETED,
    ))

    pipeline = ExtractionPipeline(
        ledger=ledger,
        context_store=context_store,
        inference_router=inference,
        north_home=tmp_path,
    )
    await pipeline.run_once()

    watermark_file = tmp_path / "extraction_watermark.txt"
    assert watermark_file.exists()
    assert watermark_file.read_text().strip()


# ---------------------------------------------------------------------------
# Privacy enforcement
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_allowed_documents_defaults_without_rules(tmp_path):
    """When privacy_rules.md is empty, _allowed_documents() returns the safe defaults."""
    from agents.base import Agent
    from agents.models import AgentConfig
    from context.models import ContextDocument

    context_store = FileContextStore(tmp_path / "context")
    inference = MockInferenceRouter()
    tool_registry = ToolRegistry(graph={}, auto_register=False)
    confidence_tracker = ConfidenceTracker(db_path=tmp_path / "tools.db")

    agent_deps = AgentDependencies(
        context_store=context_store,
        inference_router=inference,
        tool_registry=tool_registry,
        confidence_tracker=confidence_tracker,
    )

    # Concrete subclass just enough to test _allowed_documents
    class _TestAgent(Agent):
        async def _execute(self, payload, context, scored_tools):
            return {"output": "", "summary": "", "data": {}, "requires_approval": False,
                    "has_question": False, "question": None, "question_options": [], "cost_usd": 0.0}

    config = AgentConfig(agent="health", domain="health", model_pool="fast_cheap")
    agent = _TestAgent(config, agent_deps)

    docs = await agent._allowed_documents()
    assert ContextDocument.PUBLIC in docs
    assert ContextDocument.JUDGEMENT_RULES in docs


@pytest.mark.asyncio
async def test_allowed_documents_respects_rules(tmp_path):
    """When privacy_rules.md has a matching rule, _allowed_documents() returns only those docs."""
    from agents.base import Agent
    from agents.models import AgentConfig
    from context.models import ContextDocument

    context_store = FileContextStore(tmp_path / "context")
    # Write a rule that restricts 'coder' to only public.md
    rules_path = tmp_path / "context" / "privacy_rules.md"
    rules_path.parent.mkdir(parents=True, exist_ok=True)
    rules_path.write_text("coder: can_read: public.md\n", encoding="utf-8")

    inference = MockInferenceRouter()
    tool_registry = ToolRegistry(graph={}, auto_register=False)
    confidence_tracker = ConfidenceTracker(db_path=tmp_path / "tools.db")

    agent_deps = AgentDependencies(
        context_store=context_store,
        inference_router=inference,
        tool_registry=tool_registry,
        confidence_tracker=confidence_tracker,
    )

    class _TestAgent(Agent):
        async def _execute(self, payload, context, scored_tools):
            return {"output": "", "summary": "", "data": {}, "requires_approval": False,
                    "has_question": False, "question": None, "question_options": [], "cost_usd": 0.0}

    config = AgentConfig(agent="coder", domain="engineering", model_pool="fast_cheap")
    agent = _TestAgent(config, agent_deps)

    docs = await agent._allowed_documents()
    assert docs == [ContextDocument.PUBLIC]
    assert ContextDocument.JUDGEMENT_RULES not in docs


# ---------------------------------------------------------------------------
# Delegation depth guard
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_delegate_task_blocked_at_depth_limit(tmp_path):
    """_delegate_task() must return a failure JSON when delegation_depth >= limit."""
    import json

    from agents.agentic_llm_agent import _MAX_DELEGATION_DEPTH, AgenticLLMAgent
    from agents.models import AgentConfig

    context_store = FileContextStore(tmp_path / "context")
    inference = MockInferenceRouter()
    tool_registry = ToolRegistry(graph={}, auto_register=False)
    confidence_tracker = ConfidenceTracker(db_path=tmp_path / "tools.db")

    agent_deps = AgentDependencies(
        context_store=context_store,
        inference_router=inference,
        tool_registry=tool_registry,
        confidence_tracker=confidence_tracker,
    )

    config = AgentConfig(agent="general", domain="general", model_pool="fast_cheap")
    agent = AgenticLLMAgent(config, agent_deps)

    deep_payload = AgentPayload(
        task_id="t1",
        prompt="do something",
        delegation_depth=_MAX_DELEGATION_DEPTH,  # already at the limit
    )

    result_str = await agent._delegate_task(deep_payload, {"agent": "general", "task": "do something"})
    result = json.loads(result_str)
    assert result["success"] is False
    assert "depth limit" in result["error"].lower()


# ---------------------------------------------------------------------------
# Episodic store pruning
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_episodic_store_prunes_old_entries(tmp_path):
    """record() must remove episodes beyond the retention window."""
    import datetime

    from context.episodic import _RETENTION_DAYS, EpisodicStore
    from utils.db import open_db_connection

    store = EpisodicStore(db_path=tmp_path / "episodic.db")

    # Insert a row with an old timestamp directly (bypassing record() to avoid
    # the prune running before we verify it works)
    old_ts = (datetime.datetime.utcnow() - datetime.timedelta(days=_RETENTION_DAYS + 1)).isoformat()
    db = tmp_path / "episodic.db"

    def _insert_old():
        with open_db_connection(db) as conn:
            conn.execute(
                "INSERT INTO episodes (id, task_id, domain, summary, embedding, timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("old-ep", "old-task", "general", "old summary", None, old_ts),
            )
            conn.commit()

    await asyncio.to_thread(_insert_old)

    # Verify old entry exists before record()
    def _count(ep_id):
        with open_db_connection(db) as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM episodes WHERE id = ?", (ep_id,)
            ).fetchone()[0]

    assert await asyncio.to_thread(_count, "old-ep") == 1

    # record() a new episode — this should trigger pruning
    await store.record("new-task", "general", "new summary")

    assert await asyncio.to_thread(_count, "old-ep") == 0
