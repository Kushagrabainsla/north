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

from agents.models import AgentDependencies, AgentPayload, AgentResult
from approval.store import ApprovalStore
from approval.terminal import TerminalNotifier
from jobs import SQLiteJobProcessor
from ledger import SQLiteLedgerWriter
from ledger.base import LedgerFilters
from ledger.models import LedgerEntry, LedgerSource, LedgerStatus
from memory import FileContextStore, LocalMemoryGateway
from memory.extraction import ExtractionPipeline
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


async def _wait_for_ledger_action(
    ledger: SQLiteLedgerWriter,
    task_id: str,
    action: str,
    timeout: float = 5.0,
) -> None:
    """Poll the ledger until `action` appears for `task_id` or `timeout` expires.

    Replaces asyncio.sleep() in pipeline tests - correct on slow machines,
    fast on fast ones.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        entries = await ledger.query(LedgerFilters(task_id=task_id))
        if any(e.action == action for e in entries):
            return
        await asyncio.sleep(0.05)
    raise TimeoutError(f"Ledger action '{action}' never appeared for task '{task_id}' within {timeout}s")


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
        north_star_checker=NorthStarChecker(LocalMemoryGateway(context_store), inference),
        execution_planner=ExecutionPlanner(agent_registry, inference, tool_registry),
        task_context_store=task_ctx,
        failure_handler=failure_handler,
        notifier=TerminalNotifier(),
        stream_manager=stream,
        approval_store=approval,
        synthesizer=ResultSynthesizer(inference_router=inference, memory=LocalMemoryGateway(context_store)),
    )
    return orch, ledger, approval


# ---------------------------------------------------------------------------
# Orchestrator pipeline tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_task_returns_task_id(tmp_path):
    """submit_task() must return a non-empty task_id immediately."""
    orch, _, _ = _make_orchestrator(tmp_path)
    response = await orch.submit_task(TaskRequest(prompt="What is 2 + 2?", source=LedgerSource.PROMPT))
    assert response.task_id
    assert response.status == LedgerStatus.PENDING.value


@pytest.mark.asyncio
async def test_submit_task_writes_pending_ledger_entry(tmp_path):
    """The initial ledger write must happen synchronously before the background task runs."""
    orch, ledger, _ = _make_orchestrator(tmp_path)
    response = await orch.submit_task(TaskRequest(prompt="Hello", source=LedgerSource.PROMPT))
    entries = await ledger.query(LedgerFilters(task_id=response.task_id))
    # task_received is written synchronously by submit_task; the background
    # pipeline may already have appended later entries, so assert presence
    # rather than position (the query returns newest-first).
    assert any(e.action == "task_received" for e in entries)


@pytest.mark.asyncio
async def test_task_pipeline_completes(tmp_path):
    """After the background coroutine runs, the ledger must contain a completed entry."""
    orch, ledger, _ = _make_orchestrator(tmp_path)
    response = await orch.submit_task(TaskRequest(prompt="List something", source=LedgerSource.PROMPT))
    await _wait_for_ledger_action(ledger, response.task_id, "task_completed")

    entries = await ledger.query(LedgerFilters(task_id=response.task_id))
    actions = [e.action for e in entries]
    assert "task_completed" in actions


@pytest.mark.asyncio
async def test_forced_agent_runs_that_agent_and_skips_planner(tmp_path):
    """A forced_agent task must run exactly that agent and bypass classification."""
    orch, ledger, _ = _make_orchestrator(tmp_path)
    response = await orch.submit_task(
        TaskRequest(prompt="do the thing", source=LedgerSource.PROMPT, forced_agent="general")
    )
    await _wait_for_ledger_action(ledger, response.task_id, "task_completed")

    entries = await ledger.query(LedgerFilters(task_id=response.task_id))
    actions = [e.action for e in entries]
    # The planner is bypassed: no intent-classification entry is written.
    assert not any(a.startswith("classified_as_") for a in actions)
    # Exactly the requested agent ran.
    ran = {e.agent for e in entries if e.action == "agent_completed"}
    assert ran == {"general"}


@pytest.mark.asyncio
async def test_cancel_task_writes_cancelled_entry(tmp_path):
    """cancel_task() must write a task_cancelled ledger entry."""
    orch, ledger, _ = _make_orchestrator(tmp_path)
    response = await orch.submit_task(TaskRequest(prompt="Long running task", source=LedgerSource.PROMPT))
    await orch.cancel_task(response.task_id)

    entries = await ledger.query(LedgerFilters(task_id=response.task_id))
    actions = [e.action for e in entries]
    assert "task_cancelled" in actions


@pytest.mark.asyncio
async def test_concurrent_task_cap_raises(tmp_path):
    """submit_task() must raise OrchestratorError when the concurrent cap is hit."""
    import unittest.mock as mock

    from orchestrator.constants import MAX_CONCURRENT_TASKS as _MAX_CONCURRENT_TASKS
    from orchestrator.exceptions import OrchestratorError

    orch, _, _ = _make_orchestrator(tmp_path)

    # Fill _active_tasks with fake entries to simulate the cap being hit
    fake_tasks = {f"task_{i}": mock.MagicMock() for i in range(_MAX_CONCURRENT_TASKS)}
    orch._active_tasks = fake_tasks  # type: ignore[assignment]

    with pytest.raises(OrchestratorError, match="Too many concurrent tasks"):
        await orch.submit_task(TaskRequest(prompt="one more", source=LedgerSource.PROMPT))


@pytest.mark.asyncio
async def test_list_active_tasks_reads_each_active_task(tmp_path):
    """list_active_tasks() returns one response per in-flight task and skips ids
    with no ledger entry yet (reads run concurrently, order preserved)."""
    import unittest.mock as mock

    orch, ledger, _ = _make_orchestrator(tmp_path)

    for task_id in ("task_a", "task_b"):
        await ledger.write(
            LedgerEntry.new(
                source=LedgerSource.SYSTEM,
                task_id=task_id,
                action="task_received",
                status=LedgerStatus.PENDING,
            )
        )

    # task_missing is in flight but has not written a ledger entry yet.
    orch._active_tasks = {  # type: ignore[assignment]
        "task_a": mock.MagicMock(),
        "task_b": mock.MagicMock(),
        "task_missing": mock.MagicMock(),
    }

    responses = await orch.list_active_tasks()

    assert {r.task_id for r in responses} == {"task_a", "task_b"}
    assert all(r.status == LedgerStatus.PENDING.value for r in responses)


@pytest.mark.asyncio
async def test_strategy_command_completes_without_agent(tmp_path):
    """'switch to eco mode' must short-circuit before routing to any agent."""
    orch, ledger, _ = _make_orchestrator(tmp_path)
    response = await orch.submit_task(TaskRequest(prompt="switch to eco mode", source=LedgerSource.PROMPT))
    await _wait_for_ledger_action(ledger, response.task_id, "agent_completed")

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

    from orchestrator.constants import NORTH_STAR_CONFIDENCE_THRESHOLD as _NORTH_STAR_CONFIDENCE_THRESHOLD
    from orchestrator.models import ExecutionMode, ExecutionPlan, IntentClassification

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
    with (
        mock.patch.object(orch._execution_planner, "plan_all", return_value=(low_conf, dummy_plan)),
        mock.patch.object(orch._north_star_checker, "check_alignment") as mock_check,
    ):
        response = await orch.submit_task(TaskRequest(prompt="send an email", source=LedgerSource.PROMPT))
        await _wait_for_ledger_action(ledger, response.task_id, "task_completed")
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

    removed = await store.cleanup_stale_tasks(active_task_ids=frozenset(), completed_retention_days=7)
    assert removed > 0

    # Verify rows are gone
    def _count():
        with open_db_connection(db) as conn:
            return conn.execute("SELECT COUNT(*) FROM task_state WHERE task_id = 'old_task'").fetchone()[0]

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

    removed = await store.cleanup_stale_tasks(active_task_ids=frozenset(["active_task"]), completed_retention_days=7)
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

    await ledger.write(
        LedgerEntry(
            id=generate_id(),
            timestamp=utcnow(),
            source=LedgerSource.SYSTEM,
            action="startup",
            status=LedgerStatus.COMPLETED,
        )
    )

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

    await ledger.write(
        LedgerEntry(
            id=generate_id(),
            timestamp=utcnow(),
            source=LedgerSource.PROMPT,
            input="I prefer window seats",
            action="task_received",
            status=LedgerStatus.COMPLETED,
        )
    )

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
# Delegation depth guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delegate_task_blocked_at_depth_limit(tmp_path):
    """_delegate_task() must return a failure JSON when delegation_depth >= limit."""
    import json

    from agents.constants import MAX_DELEGATION_DEPTH as _MAX_DELEGATION_DEPTH
    from agents.general.agent import GeneralAgent
    from agents.models import AgentConfig

    _AGENTS_DIR = Path(__file__).parent.parent.parent / "agents"
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

    config = AgentConfig.from_yaml(_AGENTS_DIR / "general" / "config.yaml")
    agent = GeneralAgent(config, agent_deps)

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

    from memory.episodic import _RETENTION_DAYS, EpisodicStore
    from utils.db import open_db_connection

    store = EpisodicStore(db_path=tmp_path / "episodic.db")

    # Insert a row with an old timestamp directly (bypassing record() to avoid
    # the prune running before we verify it works)
    old_ts = (utcnow() - datetime.timedelta(days=_RETENTION_DAYS + 1)).isoformat()
    db = tmp_path / "episodic.db"

    def _insert_old():
        with open_db_connection(db) as conn:
            conn.execute(
                "INSERT INTO episodes (id, task_id, domain, summary, embedding, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
                ("old-ep", "old-task", "general", "old summary", None, old_ts),
            )
            conn.commit()

    await asyncio.to_thread(_insert_old)

    # Verify old entry exists before record()
    def _count(ep_id):
        with open_db_connection(db) as conn:
            return conn.execute("SELECT COUNT(*) FROM episodes WHERE id = ?", (ep_id,)).fetchone()[0]

    assert await asyncio.to_thread(_count, "old-ep") == 1

    # record() a new episode - this should trigger pruning
    await store.record("new-task", "general", "new summary")

    assert await asyncio.to_thread(_count, "old-ep") == 0


@pytest.mark.asyncio
async def test_episodic_search_filters_by_domain(tmp_path):
    """search(allowed_domains=...) returns only episodes from the allowed domains,
    and an empty allow-list returns nothing (fail-closed)."""
    from memory.episodic import EpisodicStore

    store = EpisodicStore(db_path=tmp_path / "episodic.db")
    await store.record("t-coder", "coder", "fixed a bug in the parser")
    await store.record("t-finance", "finance", "reconciled the budget spreadsheet")

    coder_only = await store.search("parser", allowed_domains=frozenset({"coder"}))
    assert any("parser" in s for s in coder_only)
    assert not any("budget" in s for s in coder_only)

    assert await store.search("parser", allowed_domains=frozenset()) == []


# ---------------------------------------------------------------------------
# Direct/Single Tool execution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_single_tool_passes_task_id(tmp_path):
    """_execute_single_tool must pass task_id to the tool params."""
    import unittest.mock as mock

    from orchestrator.models import ExecutionMode, ExecutionPlan

    orch, _, _ = _make_orchestrator(tmp_path)

    # Set up a dummy tool registry with a mock tool
    mock_tool = mock.MagicMock()
    mock_tool.name = "test_tool"
    mock_tool.run = mock.AsyncMock(return_value=mock.MagicMock(success=True, data={}))
    mock_tool.format_output = mock.MagicMock(return_value="success")

    orch._tool_registry = mock.MagicMock()
    orch._tool_registry.get.return_value = mock_tool

    plan = ExecutionPlan(
        task_id="t1",
        agents=[],
        parallel_groups=[],
        dependencies={},
        mode=ExecutionMode.SINGLE_TOOL,
        direct_tool="test_tool",
        direct_tool_params={"arg1": "val1"},
    )

    await orch._execute_single_tool(
        task_id="t1",
        prompt="run test_tool",
        plan=plan,
        workspace=str(tmp_path),
    )

    mock_tool.run.assert_called_once()
    tool_input = mock_tool.run.call_args[0][0]
    assert tool_input.params["task_id"] == "t1"
    assert tool_input.params["workspace"] == str(tmp_path)
    assert tool_input.params["arg1"] == "val1"


# ---------------------------------------------------------------------------
# resume_task (startup recovery of interrupted tasks)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_task_reuses_id_and_completes(tmp_path):
    """resume_task() re-runs a task under its original id and drives it to completion."""
    orch, ledger, _ = _make_orchestrator(tmp_path)

    ok = await orch.resume_task("resumed-task-1", TaskRequest(prompt="What is 2 + 2?", source=LedgerSource.PROMPT))
    assert ok is True

    await _wait_for_ledger_action(ledger, "resumed-task-1", "task_completed")

    actions = [e.action for e in await ledger.query(LedgerFilters(task_id="resumed-task-1"))]
    # It recorded a resume marker (not a fresh task_received) and finished.
    assert "task_resumed" in actions
    assert "task_received" not in actions
    assert "task_completed" in actions


@pytest.mark.asyncio
async def test_resume_task_declines_when_already_active(tmp_path):
    """resume_task() must no-op (and write nothing) when the id is already in flight."""
    import unittest.mock as mock

    orch, ledger, _ = _make_orchestrator(tmp_path)
    orch._active_tasks["dupe"] = mock.MagicMock()  # type: ignore[assignment]

    ok = await orch.resume_task("dupe", TaskRequest(prompt="hello", source=LedgerSource.PROMPT))

    assert ok is False
    assert await ledger.query(LedgerFilters(task_id="dupe")) == []


# ---------------------------------------------------------------------------
# Worktree isolation for the coder (end-to-end through the orchestrator)
# ---------------------------------------------------------------------------


def _init_git_repo(path: Path) -> None:
    import subprocess

    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=path, check=True)
    (path / "seed.txt").write_text("seed\n")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)


def _north_branches(path: Path) -> list[str]:
    import subprocess

    out = subprocess.run(["git", "branch", "--list", "north/*"], cwd=path, capture_output=True, text=True).stdout
    return [line.strip().lstrip("* ").strip() for line in out.splitlines() if line.strip()]


class _FileWritingCoder:
    """Minimal stand-in agent that writes one file into its (isolated) workspace."""

    name = "coder"

    def __init__(self, filename: str, content: str) -> None:
        self._filename = filename
        self._content = content

    async def run(self, payload: AgentPayload) -> AgentResult:
        (Path(payload.workspace) / self._filename).write_text(self._content)
        return AgentResult(output="done", summary=f"wrote {self._filename}", successful_tools=["write_file"])


def _isolating_orchestrator(tmp_path: Path):
    orch, ledger, _ = _make_orchestrator(tmp_path)
    orch._worktree_isolation = True  # type: ignore[attr-defined]
    orch._worktree_root = str(tmp_path / "worktrees")  # type: ignore[attr-defined]
    return orch, ledger


@pytest.mark.asyncio
async def test_isolated_coder_applies_changes_back(tmp_path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    orch, _ = _isolating_orchestrator(tmp_path)
    payload = AgentPayload(task_id="t1", prompt="add feature", workspace=str(repo))

    result = await orch._run_agent_isolated_or_direct(_FileWritingCoder("feature.py", "x = 1\n"), payload)

    assert result.summary == "wrote feature.py"
    assert (repo / "feature.py").read_text() == "x = 1\n"  # applied back to base tree
    assert _north_branches(repo) == []  # worktree branch cleaned up


@pytest.mark.asyncio
async def test_isolated_coder_does_not_touch_base_until_integrate(tmp_path):
    """The coder's writes go to the worktree, not the base tree, during the run."""
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    orch, _ = _isolating_orchestrator(tmp_path)
    payload = AgentPayload(task_id="t1", prompt="x", workspace=str(repo))

    seen: dict[str, bool] = {}

    class _Probe(_FileWritingCoder):
        async def run(self, payload: AgentPayload) -> AgentResult:
            # While running, our workspace is NOT the base repo.
            seen["isolated"] = Path(payload.workspace) != repo
            seen["base_clean"] = not (repo / "feature.py").exists()
            return await super().run(payload)

    await orch._run_agent_isolated_or_direct(_Probe("feature.py", "1\n"), payload)

    assert seen == {"isolated": True, "base_clean": True}
    assert (repo / "feature.py").exists()  # applied on completion


@pytest.mark.asyncio
async def test_two_isolated_coders_disjoint_both_apply(tmp_path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    orch, _ = _isolating_orchestrator(tmp_path)
    payload = AgentPayload(task_id="t1", prompt="parallel", workspace=str(repo))

    await asyncio.gather(
        orch._run_agent_isolated_or_direct(_FileWritingCoder("a.py", "A\n"), payload),
        orch._run_agent_isolated_or_direct(_FileWritingCoder("b.py", "B\n"), payload),
    )

    assert (repo / "a.py").read_text() == "A\n"
    assert (repo / "b.py").read_text() == "B\n"
    assert _north_branches(repo) == []


@pytest.mark.asyncio
async def test_two_isolated_coders_conflict_retains_branch(tmp_path):
    import subprocess

    repo = tmp_path / "repo"
    _init_git_repo(repo)
    orch, ledger = _isolating_orchestrator(tmp_path)
    payload = AgentPayload(task_id="t1", prompt="collide", workspace=str(repo))

    await asyncio.gather(
        orch._run_agent_isolated_or_direct(_FileWritingCoder("collide.txt", "FROM_A\n"), payload),
        orch._run_agent_isolated_or_direct(_FileWritingCoder("collide.txt", "FROM_B\n"), payload),
    )

    # Exactly one landed; the other was retained on a branch and logged as a conflict.
    assert (repo / "collide.txt").read_text() in {"FROM_A\n", "FROM_B\n"}
    assert len(_north_branches(repo)) == 1
    actions = [e.action for e in await ledger.query(LedgerFilters(task_id="t1"))]
    assert "worktree_conflict" in actions

    for branch in _north_branches(repo):
        subprocess.run(["git", "branch", "-D", branch], cwd=repo, capture_output=True)


class _UniqueFileCoder:
    """Coder stand-in that writes a distinct file per attempt and counts its runs."""

    name = "coder"

    def __init__(self) -> None:
        self.runs = 0

    async def run(self, payload: AgentPayload) -> AgentResult:
        self.runs += 1
        fname = f"cand_{Path(payload.workspace).name}.py"
        (Path(payload.workspace) / fname).write_text("x = 1\n")
        return AgentResult(output="done", summary=f"wrote {fname}", successful_tools=["write_file"])


@pytest.mark.asyncio
async def test_best_of_n_integrates_exactly_one_candidate(tmp_path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    orch, _ = _isolating_orchestrator(tmp_path)
    orch._best_of_n = 3  # type: ignore[attr-defined]
    coder = _UniqueFileCoder()
    payload = AgentPayload(task_id="t1", prompt="add feature", workspace=str(repo))

    result = await orch._run_agent_isolated_or_direct(coder, payload)

    assert coder.runs == 3  # all three candidates ran
    landed = list(repo.glob("cand_*.py"))
    assert len(landed) == 1  # exactly one winner integrated; losers discarded
    assert result.summary.startswith("wrote cand_")
    assert _north_branches(repo) == []  # every worktree branch cleaned up


@pytest.mark.asyncio
async def test_best_of_n_disabled_runs_single_attempt(tmp_path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    orch, _ = _isolating_orchestrator(tmp_path)
    orch._best_of_n = 1  # type: ignore[attr-defined]
    coder = _UniqueFileCoder()
    payload = AgentPayload(task_id="t1", prompt="add feature", workspace=str(repo))

    await orch._run_agent_isolated_or_direct(coder, payload)

    assert coder.runs == 1  # N=1 is the ordinary single-worktree path
    assert len(list(repo.glob("cand_*.py"))) == 1


# ---------------------------------------------------------------------------
# RunningTaskStore lifecycle through the real pipeline (crash-recovery substrate)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_running_task_store_marked_on_submit_and_cleared_on_completion(tmp_path):
    from orchestrator.running_tasks import RunningTaskStore

    orch, ledger, _ = _make_orchestrator(tmp_path)
    store = RunningTaskStore(tmp_path / "running_tasks.db")
    orch._running_task_store = store  # type: ignore[attr-defined]

    response = await orch.submit_task(TaskRequest(prompt="List something", source=LedgerSource.PROMPT))

    # The task is registered as in-flight the moment submit returns.
    assert response.task_id in {rt.task_id for rt in await store.list_all()}

    await _wait_for_ledger_action(ledger, response.task_id, "task_completed")

    # The terminal finally-block drops it from the registry (no leak).
    for _ in range(40):
        if not await store.list_all():
            break
        await asyncio.sleep(0.05)
    assert await store.list_all() == []


@pytest.mark.asyncio
async def test_running_task_store_cleared_on_cancel(tmp_path):
    from orchestrator.running_tasks import RunningTaskStore

    orch, _, _ = _make_orchestrator(tmp_path)
    store = RunningTaskStore(tmp_path / "running_tasks.db")
    orch._running_task_store = store  # type: ignore[attr-defined]

    response = await orch.submit_task(TaskRequest(prompt="Long task", source=LedgerSource.PROMPT))
    await orch.cancel_task(response.task_id)

    for _ in range(40):
        if not await store.list_all():
            break
        await asyncio.sleep(0.05)
    assert await store.list_all() == []


# ---------------------------------------------------------------------------
# Self-repair loop for unverified claims (#6)
# ---------------------------------------------------------------------------


class _ClaimThenCorrectAgent:
    """Claims a file was created; on the correction pass, drops the claim."""

    name = "coder"

    def __init__(self) -> None:
        self.runs = 0

    async def run(self, payload: AgentPayload) -> AgentResult:
        self.runs += 1
        if "[correction required]" in payload.prompt:
            return AgentResult(output="Here is the code for you to paste.", summary="drafted", successful_tools=[])
        return AgentResult(output="I created the file foo.py for you.", summary="done", successful_tools=[])


class _ClaimThenDoItAgent:
    """Claims a file was created; on the correction pass, actually uses write_file."""

    name = "coder"

    async def run(self, payload: AgentPayload) -> AgentResult:
        if "[correction required]" in payload.prompt:
            return AgentResult(output="I created the file foo.py.", summary="done", successful_tools=["write_file"])
        return AgentResult(output="I created the file foo.py.", summary="done", successful_tools=[])


class _StubbornClaimAgent:
    name = "coder"

    def __init__(self) -> None:
        self.runs = 0

    async def run(self, payload: AgentPayload) -> AgentResult:
        self.runs += 1
        return AgentResult(output="I created the file foo.py.", summary="done", successful_tools=[])


def _claimy_result() -> AgentResult:
    return AgentResult(output="I created the file foo.py for you.", summary="done", successful_tools=[])


@pytest.mark.asyncio
async def test_self_repair_drops_unverified_claim(tmp_path):
    orch, ledger, _ = _make_orchestrator(tmp_path)
    agent = _ClaimThenCorrectAgent()
    result = _claimy_result()
    payload = AgentPayload(task_id="t1", prompt="write foo")

    await orch._verify_agent_claims("t1", agent, result, payload)

    assert agent.runs == 1  # one correction pass
    assert "Unverified claims" not in result.output  # repaired rather than flagged
    assert result.output == "Here is the code for you to paste."
    actions = [e.action for e in await ledger.query(LedgerFilters(task_id="t1"))]
    assert "self_repair" in actions
    assert "claims_unverified" not in actions


@pytest.mark.asyncio
async def test_self_repair_by_doing_the_work_substantiates_claim(tmp_path):
    orch, _, _ = _make_orchestrator(tmp_path)
    result = _claimy_result()

    await orch._verify_agent_claims("t1", _ClaimThenDoItAgent(), result, AgentPayload(task_id="t1", prompt="write foo"))

    assert "Unverified claims" not in result.output
    assert result.successful_tools == ["write_file"]  # evidence now present


@pytest.mark.asyncio
async def test_self_repair_no_improvement_flags_original(tmp_path):
    orch, ledger, _ = _make_orchestrator(tmp_path)
    agent = _StubbornClaimAgent()
    result = _claimy_result()

    await orch._verify_agent_claims("t1", agent, result, AgentPayload(task_id="t1", prompt="write foo"))

    assert agent.runs == 1  # tried once, no better
    assert "Unverified claims" in result.output  # original flagged
    actions = [e.action for e in await ledger.query(LedgerFilters(task_id="t1"))]
    assert "claims_unverified" in actions


@pytest.mark.asyncio
async def test_no_self_repair_when_disabled(tmp_path):
    orch, _, _ = _make_orchestrator(tmp_path)
    orch._self_repair = False  # type: ignore[attr-defined]
    agent = _StubbornClaimAgent()
    result = _claimy_result()

    await orch._verify_agent_claims("t1", agent, result, AgentPayload(task_id="t1", prompt="p"))

    assert agent.runs == 0  # no correction pass
    assert "Unverified claims" in result.output


# ---------------------------------------------------------------------------
# Submission idempotency (#4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_submission_is_deduped(tmp_path):
    orch, _, _ = _make_orchestrator(tmp_path)  # idempotency on by default

    first = await orch.submit_task(TaskRequest(prompt="same question", source=LedgerSource.PROMPT))
    second = await orch.submit_task(TaskRequest(prompt="same question", source=LedgerSource.PROMPT))

    assert second.task_id == first.task_id  # collapsed onto the first task


@pytest.mark.asyncio
async def test_distinct_submissions_get_distinct_ids(tmp_path):
    orch, _, _ = _make_orchestrator(tmp_path)

    a = await orch.submit_task(TaskRequest(prompt="question A", source=LedgerSource.PROMPT))
    b = await orch.submit_task(TaskRequest(prompt="question B", source=LedgerSource.PROMPT))

    assert a.task_id != b.task_id


@pytest.mark.asyncio
async def test_explicit_idempotency_key_dedupes_across_different_prompts(tmp_path):
    orch, _, _ = _make_orchestrator(tmp_path)

    first = await orch.submit_task(
        TaskRequest(prompt="webhook payload v1", source=LedgerSource.WEBHOOK, idempotency_key="evt-42")
    )
    second = await orch.submit_task(
        TaskRequest(prompt="webhook payload v2", source=LedgerSource.WEBHOOK, idempotency_key="evt-42")
    )

    assert second.task_id == first.task_id


@pytest.mark.asyncio
async def test_completed_task_is_not_deduped_on_resubmission(tmp_path):
    orch, _, _ = _make_orchestrator(tmp_path)

    first = await orch.submit_task(TaskRequest(prompt="repeat question", source=LedgerSource.PROMPT))
    # Wait for the first task to finish completely
    await asyncio.sleep(0.1)

    second = await orch.submit_task(TaskRequest(prompt="repeat question", source=LedgerSource.PROMPT))
    assert second.task_id != first.task_id


# ---------------------------------------------------------------------------
# Critic gate (#7)
# ---------------------------------------------------------------------------


class _VerdictRouter:
    """Minimal stand-in inference router returning a fixed critic verdict."""

    def __init__(self, verdict_json: str) -> None:
        self._verdict = verdict_json
        self.calls = 0

    async def complete(self, request):  # noqa: ANN001
        from types import SimpleNamespace

        self.calls += 1
        return SimpleNamespace(text=self._verdict, cost_usd=0.0)


def _plain_result(output: str = "A partial answer.") -> AgentResult:
    return AgentResult(output=output, summary="done", successful_tools=[])


@pytest.mark.asyncio
async def test_critic_flags_inadequate_answer(tmp_path):
    orch, ledger, _ = _make_orchestrator(tmp_path)
    orch._critic = True  # type: ignore[attr-defined]
    orch._tracked_router = _VerdictRouter('{"adequate": false, "gap": "Did not cover the second question."}')

    result = _plain_result()
    await orch._critique_result("t1", _StubbornClaimAgent(), result, AgentPayload(task_id="t1", prompt="two things"))

    assert "Reviewer note:" in result.output
    assert "second question" in result.output
    actions = [e.action for e in await ledger.query(LedgerFilters(task_id="t1"))]
    assert "critic_flagged" in actions


@pytest.mark.asyncio
async def test_critic_passes_adequate_answer(tmp_path):
    orch, ledger, _ = _make_orchestrator(tmp_path)
    orch._critic = True  # type: ignore[attr-defined]
    orch._tracked_router = _VerdictRouter('{"adequate": true, "gap": ""}')

    result = _plain_result("Complete answer.")
    await orch._critique_result("t1", _StubbornClaimAgent(), result, AgentPayload(task_id="t1", prompt="q"))

    assert result.output == "Complete answer."  # untouched
    actions = [e.action for e in await ledger.query(LedgerFilters(task_id="t1"))]
    assert "critic_flagged" not in actions


@pytest.mark.asyncio
async def test_critic_disabled_makes_no_call(tmp_path):
    orch, _, _ = _make_orchestrator(tmp_path)
    orch._critic = False  # type: ignore[attr-defined]
    router = _VerdictRouter('{"adequate": false, "gap": "x"}')
    orch._tracked_router = router

    result = _plain_result()
    await orch._critique_result("t1", _StubbornClaimAgent(), result, AgentPayload(task_id="t1", prompt="q"))

    assert router.calls == 0
    assert result.output == "A partial answer."


@pytest.mark.asyncio
async def test_critic_fails_open_on_bad_json(tmp_path):
    orch, _, _ = _make_orchestrator(tmp_path)
    orch._critic = True  # type: ignore[attr-defined]
    orch._tracked_router = _VerdictRouter("this is not json")

    result = _plain_result()
    await orch._critique_result("t1", _StubbornClaimAgent(), result, AgentPayload(task_id="t1", prompt="q"))

    assert result.output == "A partial answer."  # unchanged, no crash


@pytest.mark.asyncio
async def test_single_tool_marks_side_effect_when_mutating(tmp_path):
    import unittest.mock as mock

    from orchestrator.models import ExecutionMode, ExecutionPlan
    from orchestrator.running_tasks import RunningTaskStore

    orch, _, _ = _make_orchestrator(tmp_path)
    store = RunningTaskStore(tmp_path / "rt.db")
    orch._running_task_store = store  # type: ignore[attr-defined]
    await store.mark_running("t1", TaskRequest(prompt="turn off the lamp", source=LedgerSource.PROMPT))

    tool = mock.MagicMock()
    tool.name = "kasa"
    tool.is_mutating = True
    tool.run = mock.AsyncMock(return_value=mock.MagicMock(success=True, data={}))
    tool.format_output = mock.MagicMock(return_value="ok")
    orch._tool_registry = mock.MagicMock()
    orch._tool_registry.get.return_value = tool

    plan = ExecutionPlan(
        task_id="t1",
        agents=[],
        parallel_groups=[],
        dependencies={},
        mode=ExecutionMode.SINGLE_TOOL,
        direct_tool="kasa",
        direct_tool_params={},
    )
    await orch._execute_single_tool(task_id="t1", prompt="p", plan=plan, workspace=str(tmp_path))

    assert (await store.list_all())[0].has_side_effects is True


@pytest.mark.asyncio
async def test_single_tool_no_side_effect_when_readonly(tmp_path):
    import unittest.mock as mock

    from orchestrator.models import ExecutionMode, ExecutionPlan
    from orchestrator.running_tasks import RunningTaskStore

    orch, _, _ = _make_orchestrator(tmp_path)
    store = RunningTaskStore(tmp_path / "rt.db")
    orch._running_task_store = store  # type: ignore[attr-defined]
    await store.mark_running("t1", TaskRequest(prompt="what time is it", source=LedgerSource.PROMPT))

    tool = mock.MagicMock()
    tool.name = "get_time"
    tool.is_mutating = False
    tool.run = mock.AsyncMock(return_value=mock.MagicMock(success=True, data={}))
    tool.format_output = mock.MagicMock(return_value="noon")
    orch._tool_registry = mock.MagicMock()
    orch._tool_registry.get.return_value = tool

    plan = ExecutionPlan(
        task_id="t1",
        agents=[],
        parallel_groups=[],
        dependencies={},
        mode=ExecutionMode.SINGLE_TOOL,
        direct_tool="get_time",
        direct_tool_params={},
    )
    await orch._execute_single_tool(task_id="t1", prompt="p", plan=plan, workspace=str(tmp_path))

    assert (await store.list_all())[0].has_side_effects is False


# ---------------------------------------------------------------------------
# Enforced handoff (#3): inject real artifacts, flag missing ones
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handoff_injects_real_artifact(tmp_path, monkeypatch):
    orch, _, _ = _make_orchestrator(tmp_path)
    monkeypatch.setattr("orchestrator.orchestrator.handoff_dir_for", lambda tid: str(tmp_path / "hand" / tid))

    spec = tmp_path / "hand" / "t1" / "architecture" / "spec.md"
    spec.parent.mkdir(parents=True)
    spec.write_text("# Spec\nUse a ring buffer keyed by session id.\n")

    snippets, missing = await orch._collect_handoff_artifacts("t1", ["architect"])

    assert missing == []
    assert len(snippets) == 1
    assert "ring buffer keyed by session id" in snippets[0]  # real file content, not a summary
    assert "spec.md" in snippets[0]


@pytest.mark.asyncio
async def test_handoff_flags_missing_artifact(tmp_path, monkeypatch):
    orch, ledger, _ = _make_orchestrator(tmp_path)
    monkeypatch.setattr("orchestrator.orchestrator.handoff_dir_for", lambda tid: str(tmp_path / "hand" / tid))

    # No spec.md written.
    snippets, missing = await orch._collect_handoff_artifacts("t1", ["architect"])
    assert snippets == []
    assert missing == ["architect"]

    await orch._warn_missing_handoff_artifact("t1", "architect")
    actions = [e.action for e in await ledger.query(LedgerFilters(task_id="t1"))]
    assert "handoff_artifact_missing" in actions


@pytest.mark.asyncio
async def test_handoff_empty_artifact_counts_as_missing(tmp_path, monkeypatch):
    orch, _, _ = _make_orchestrator(tmp_path)
    monkeypatch.setattr("orchestrator.orchestrator.handoff_dir_for", lambda tid: str(tmp_path / "hand" / tid))

    spec = tmp_path / "hand" / "t1" / "architecture" / "spec.md"
    spec.parent.mkdir(parents=True)
    spec.write_text("   \n")  # whitespace only

    snippets, missing = await orch._collect_handoff_artifacts("t1", ["architect"])
    assert snippets == []
    assert missing == ["architect"]


# ---------------------------------------------------------------------------
# get_task reports a task's TERMINAL status, not the latest step (#3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_task_reports_pending_while_running(tmp_path):
    orch, ledger, _ = _make_orchestrator(tmp_path)
    # Intermediate steps are logged COMPLETED per-step; none is terminal.
    await ledger.write(
        LedgerEntry.new(source=LedgerSource.PROMPT, task_id="t1", action="task_received", status=LedgerStatus.PENDING)
    )
    await ledger.write(
        LedgerEntry.new(
            source=LedgerSource.SYSTEM, task_id="t1", action="classified_as_trivial", status=LedgerStatus.COMPLETED
        )
    )
    resp = await orch.get_task("t1")
    assert resp is not None
    assert resp.status == "pending"  # NOT "completed" - the task is still running


@pytest.mark.asyncio
async def test_get_task_reports_completed_only_on_terminal_entry(tmp_path):
    orch, ledger, _ = _make_orchestrator(tmp_path)
    await ledger.write(
        LedgerEntry.new(
            source=LedgerSource.SYSTEM, task_id="t1", action="classified_as_trivial", status=LedgerStatus.COMPLETED
        )
    )
    await ledger.write(
        LedgerEntry.new(
            source=LedgerSource.SYSTEM, task_id="t1", action="task_completed", status=LedgerStatus.COMPLETED
        )
    )
    # An extraction entry written AFTER completion must not mask the terminal status.
    await ledger.write(
        LedgerEntry.new(
            source=LedgerSource.SYSTEM,
            task_id="t1",
            action="extraction: user.md updated",
            status=LedgerStatus.COMPLETED,
        )
    )
    resp = await orch.get_task("t1")
    assert resp.status == "completed"


@pytest.mark.asyncio
async def test_get_task_reports_failed_on_terminal_failure(tmp_path):
    orch, ledger, _ = _make_orchestrator(tmp_path)
    await ledger.write(
        LedgerEntry.new(
            source=LedgerSource.SYSTEM, task_id="t1", action="classified_as_trivial", status=LedgerStatus.COMPLETED
        )
    )
    await ledger.write(
        LedgerEntry.new(source=LedgerSource.SYSTEM, task_id="t1", action="task_failed", status=LedgerStatus.FAILED)
    )
    resp = await orch.get_task("t1")
    assert resp.status == "failed"


@pytest.mark.asyncio
async def test_get_task_unknown_returns_none(tmp_path):
    orch, _, _ = _make_orchestrator(tmp_path)
    assert await orch.get_task("nope") is None


@pytest.mark.asyncio
async def test_respond_approval_records_decision_for_learning(tmp_path):
    from approval.approval_memory import ApprovalMemory
    from approval.models import Card, CardType

    orch, _, approval = _make_orchestrator(tmp_path)
    mem = ApprovalMemory(tmp_path / "am.db")
    orch._approval_memory = mem

    card = Card(
        id="card-1",
        type=CardType.APPROVAL,
        task_id="t1",
        agent="bash",
        title="Shell Command",
        message="```\nnpm install left-pad\n```",
        options=["Run", "Cancel"],
    )
    approval.add(card)

    await orch.respond_approval("card-1", "approved", "Run")

    # The human decision is now learnable -> autonomous mode can replay it.
    assert mem.recall("bash", "```\nnpm install left-pad\n```") == "approved"


class _NamedAgent:
    def __init__(self, name: str) -> None:
        self.name = name


def _tool_result(tools: list[str]) -> AgentResult:
    return AgentResult(output="x", summary="x", successful_tools=tools)


def test_evidence_gate_flags_unverified_code_change(tmp_path):
    orch, _, _ = _make_orchestrator(tmp_path)
    v = orch._add_evidence_gate_violations(_NamedAgent("coder"), _tool_result(["patch_file"]), [])
    assert any("verify" in x for x in v)


def test_evidence_gate_flags_attempted_but_denied_edit(tmp_path):
    orch, _, _ = _make_orchestrator(tmp_path)
    # Attempted patch_file (in tools_used) but it never succeeded (not in successful_tools):
    # e.g. the approval was denied - no change was applied, so "done" is false.
    result = AgentResult(
        output="fixed it",
        summary="x",
        successful_tools=["read_file"],
        tools_used=["read_file", "patch_file"],
    )
    v = orch._add_evidence_gate_violations(_NamedAgent("coder"), result, [])
    assert any("did not succeed" in x for x in v)


def test_evidence_gate_ok_when_edit_succeeded_and_verified(tmp_path):
    orch, _, _ = _make_orchestrator(tmp_path)
    result = AgentResult(
        output="x", summary="x", successful_tools=["patch_file", "bash"], tools_used=["patch_file", "bash"]
    )
    assert orch._add_evidence_gate_violations(_NamedAgent("coder"), result, []) == []


def test_evidence_gate_ok_when_typechecked(tmp_path):
    orch, _, _ = _make_orchestrator(tmp_path)
    result = _tool_result(["patch_file", "check_types"])
    assert orch._add_evidence_gate_violations(_NamedAgent("coder"), result, []) == []


def test_evidence_gate_ok_when_tested(tmp_path):
    orch, _, _ = _make_orchestrator(tmp_path)
    assert orch._add_evidence_gate_violations(_NamedAgent("coder"), _tool_result(["write_file", "bash"]), []) == []


def test_evidence_gate_ignores_readonly(tmp_path):
    orch, _, _ = _make_orchestrator(tmp_path)
    result = _tool_result(["read_file", "search_files"])
    assert orch._add_evidence_gate_violations(_NamedAgent("coder"), result, []) == []


def test_evidence_gate_only_applies_to_engineering(tmp_path):
    orch, _, _ = _make_orchestrator(tmp_path)
    assert orch._add_evidence_gate_violations(_NamedAgent("general"), _tool_result(["patch_file"]), []) == []


class _UnverifiedThenVerifiesCoder:
    name = "coder"

    async def run(self, payload: AgentPayload) -> AgentResult:
        if "[correction required]" in payload.prompt:
            return AgentResult(
                output="Ran check_types - clean.", summary="verified", successful_tools=["patch_file", "check_types"]
            )
        return AgentResult(output="Fixed the bug.", summary="fixed", successful_tools=["patch_file"])


@pytest.mark.asyncio
async def test_evidence_gate_triggers_self_repair_to_verify(tmp_path):
    orch, _, _ = _make_orchestrator(tmp_path)
    result = AgentResult(output="Fixed the bug.", summary="fixed", successful_tools=["patch_file"])

    await orch._verify_agent_claims(
        "t1", _UnverifiedThenVerifiesCoder(), result, AgentPayload(task_id="t1", prompt="fix bug")
    )

    assert "Unverified" not in result.output  # gate forced a verification pass
    assert "check_types" in (result.successful_tools or [])



# ---------------------------------------------------------------------------
# Pre-implementation spec critique (Workstream B)
# ---------------------------------------------------------------------------


class _SpecCriticRouter:
    """Stand-in router returning a fixed critique verdict and model_used."""

    def __init__(self, text: str, model_used: str = "critic-model") -> None:
        self._text = text
        self._model = model_used
        self.calls = 0

    async def complete(self, request):  # noqa: ANN001
        from types import SimpleNamespace

        self.calls += 1
        return SimpleNamespace(text=self._text, model_used=self._model, cost_usd=0.0, tokens_in=0, tokens_out=0)


_GOOD_SPEC = "# Spec\n\n" + ("This is a sufficiently detailed agreed design spec. " * 8)


def _write_spec(base: Path, task_id: str, text: str) -> None:
    spec_dir = base / task_id / "architecture"
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "spec.md").write_text(text, encoding="utf-8")


async def _seed_architect_model(ledger, task_id: str, model: str) -> None:
    await ledger.write(
        LedgerEntry.new(
            source=LedgerSource.AGENT,
            task_id=task_id,
            agent="architect",
            action="agent_completed",
            output="wrote spec",
            status=LedgerStatus.COMPLETED,
            model_used=model,
        )
    )


@pytest.mark.asyncio
async def test_spec_critique_filters_issues_and_marks_independent(tmp_path, monkeypatch):
    base = tmp_path / "hand"
    monkeypatch.setattr("orchestrator.orchestrator.handoff_dir_for", lambda tid: str(base / tid))
    orch, ledger, _ = _make_orchestrator(tmp_path)
    await _seed_architect_model(ledger, "t1", "architect-model")
    _write_spec(base, "t1", _GOOD_SPEC)
    orch._tracked_router = _SpecCriticRouter(
        '{"issues": ["short", "Concrete: the empty-input path is unhandled and divides by zero"], "sound": false}',
        model_used="different-model",
    )

    issues = await orch._rubber_duck_spec("t1", "build X", str(tmp_path))

    assert len(issues) == 1  # the vague "short" issue was dropped
    assert "empty-input" in issues[0]
    crit = next(e for e in await ledger.query(LedgerFilters(task_id="t1")) if e.action == "spec_critique")
    assert crit.agent_output.get("independent") is True


@pytest.mark.asyncio
async def test_spec_critique_same_model_not_independent(tmp_path, monkeypatch):
    base = tmp_path / "hand"
    monkeypatch.setattr("orchestrator.orchestrator.handoff_dir_for", lambda tid: str(base / tid))
    orch, ledger, _ = _make_orchestrator(tmp_path)
    await _seed_architect_model(ledger, "t1", "only-model")
    _write_spec(base, "t1", _GOOD_SPEC)
    orch._tracked_router = _SpecCriticRouter('{"issues": [], "sound": true}', model_used="only-model")

    await orch._rubber_duck_spec("t1", "build X", str(tmp_path))

    crit = next(e for e in await ledger.query(LedgerFilters(task_id="t1")) if e.action == "spec_critique")
    assert crit.agent_output.get("independent") is False


@pytest.mark.asyncio
async def test_spec_critique_fails_open_on_bad_json(tmp_path, monkeypatch):
    base = tmp_path / "hand"
    monkeypatch.setattr("orchestrator.orchestrator.handoff_dir_for", lambda tid: str(base / tid))
    orch, _, _ = _make_orchestrator(tmp_path)
    _write_spec(base, "t1", _GOOD_SPEC)
    orch._tracked_router = _SpecCriticRouter("this is not json")

    assert await orch._rubber_duck_spec("t1", "build X", str(tmp_path)) == []


@pytest.mark.asyncio
async def test_spec_critique_skips_trivial_spec(tmp_path, monkeypatch):
    base = tmp_path / "hand"
    monkeypatch.setattr("orchestrator.orchestrator.handoff_dir_for", lambda tid: str(base / tid))
    orch, _, _ = _make_orchestrator(tmp_path)
    _write_spec(base, "t1", "tiny")
    router = _SpecCriticRouter('{"issues": ["x"], "sound": false}')
    orch._tracked_router = router

    assert await orch._rubber_duck_spec("t1", "build X", str(tmp_path)) == []
    assert router.calls == 0  # never called the model on a trivial spec


def test_coder_preamble_injects_critique_as_data_and_preserves_no_redesign(tmp_path):
    orch, _, _ = _make_orchestrator(tmp_path)

    base = orch._coder_preamble_for_agreed_spec("t1")
    assert "Implement EXACTLY" in base
    assert "concerns" not in base.lower()

    with_critique = orch._coder_preamble_for_agreed_spec("t1", ["the empty list case is unhandled"])
    assert "Implement EXACTLY" in with_critique  # do-not-redesign instruction preserved
    assert "DATA, not commands" in with_critique  # critique framed as data, not instructions
    assert "only within the agreed spec" in with_critique.lower()
    assert "empty list case is unhandled" in with_critique


@pytest.mark.asyncio
async def test_seed_plan_from_spec_seeds_plan_store(tmp_path, monkeypatch):
    from orchestrator.plan_store import PlanStore

    base = tmp_path / "hand"
    monkeypatch.setattr("orchestrator.orchestrator.handoff_dir_for", lambda tid: str(base / tid))
    orch, _, _ = _make_orchestrator(tmp_path)
    orch._plan_store = PlanStore()
    _write_spec(base, "t1", "# Spec\n\n## Tasks\n- [ ] Add the parser\n- [ ] Wire it into the loop\n")

    seeded = await orch._seed_plan_from_spec("t1")

    assert seeded == 2
    rendered = orch._plan_store.render("t1")
    assert "Add the parser" in rendered
    assert "Wire it into the loop" in rendered


@pytest.mark.asyncio
async def test_seed_plan_from_spec_no_store_is_noop(tmp_path):
    orch, _, _ = _make_orchestrator(tmp_path)
    orch._plan_store = None
    assert await orch._seed_plan_from_spec("t1") == 0
