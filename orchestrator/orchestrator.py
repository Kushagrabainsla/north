"""Main Orchestrator - ties Stages 1–4.

See docs/CODING_STYLE.md Sections 2.5, 4.1, 6, 10.2, 14.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from pathlib import Path
from typing import Any

from agents import Agent, AgentPayload, AgentResult
from agents.registry import AgentRegistry
from approval import ApprovalDecision, Card, CardType, JudgementFilter, Notifier, UserInteraction
from approval.approval_memory import ApprovalMemory
from approval.mode import ApprovalMode, resolve_approval_mode
from approval.store import ApprovalStore
from config.strategy import NorthSettings, StrategyMode, describe
from inference.cost_tracker import CostTracker
from inference.models import CompletionRequest, PoolPriority
from ledger import LedgerEntry, LedgerFilters, LedgerSource, LedgerStatus, LedgerWriter
from orchestrator.constants import (
    MAX_CONCURRENT_TASKS,
    MAX_QUEUE_ATTEMPTS,
    NORTH_STAR_CONFIDENCE_THRESHOLD,
    POOL_REFRESH_COOLDOWN,
    QUEUE_POLL_INTERVAL_SECONDS,
    STRATEGY_CMD_RE,
)
from orchestrator.engineering_prompts import (
    CONDUCTOR_CODER_PREAMBLE,
    CONDUCTOR_CODER_PREAMBLE_SPEC,
    CONDUCTOR_CODER_PREAMBLES,
    CONDUCTOR_FIX_PREAMBLE,
    CONDUCTOR_MAX_FIX_ROUNDS,
    CONDUCTOR_REVIEW_PROMPT,
    CONDUCTOR_REVIEW_RETRY_PROMPT,
    DEPLOY_KINDS,
    DEPLOY_PREAMBLE,
    DESIGN_ARCHITECT_PREAMBLE,
    DESIGN_KINDS,
    DESIGN_RESEARCH_PREAMBLE,
    SPEC_CRITIQUE_INJECTION,
    SPEC_CRITIQUE_PROMPT,
    SPEC_CRITIQUE_TIMEOUT_S,
    SPEC_MIN_CHARS,
    clean_issues,
    parse_spec_tasks,
)
from orchestrator.exceptions import NorthStarConflictError, OrchestratorError, TaskCapacityError
from orchestrator.failure_handler import FailureHandler, classify_error
from orchestrator.idempotency import IdempotencyCache, idempotency_key
from orchestrator.isolation import AgentIsolation
from orchestrator.journal import TaskJournal
from orchestrator.models import (
    ExecutionMode,
    ExecutionPlan,
    IntentClassification,
    TaskRequest,
    TaskResponse,
)
from orchestrator.north_star import NorthStarChecker
from orchestrator.quality_gate import QualityGate
from orchestrator.result_audit import ResultAuditor
from orchestrator.review import read_review_result
from orchestrator.router import ExecutionPlanner
from orchestrator.running_tasks import RunningTaskStore
from orchestrator.stream import EventStreamManager
from orchestrator.synthesizer import ResultSynthesizer
from orchestrator.task_context import TaskContextStore
from orchestrator.tiering import resolve_model_pool
from tools._path import handoff_dir_for
from tools.exceptions import ToolNotFoundError
from tools.models import ToolInput
from tools.registry import ToolRegistry
from utils.ids import generate_id, generate_task_id
from utils.logging import bind_task_id
from utils.tasks import spawn
from utils.text import extract_json
from utils.time import format_timestamp, utcnow

logger = logging.getLogger(__name__)

# Max characters of a handoff artifact injected into a downstream agent's context.
_HANDOFF_ARTIFACT_MAX_CHARS: int = 6000

# Engineering conductor (2e): coder→reviewer fix rounds allowed after the first
# review before the bounded loop stops and the DoD gate takes over.
# Coder framing per engineering_kind for the conductor loop. Kinds not listed use
# the default preamble. Keeps test/debug as task-framings of the ONE coder loop
# rather than separate write-agents (single continuous context wins for writes).
# Deploy/ship flow (Group E): shipping already-completed work is a distinct,
# human-gated flow - NOT a code-writing loop - so it runs a single agent with git/gh
# tools and is never handled by the conductor or the code Definition-of-Done gate.
# Interactive design phase (cockpit): for larger code kinds, when a human is available,
# clarify + agree the design BEFORE the continuous coder implements it. Small/localized
# kinds (bugfix/debug/test) skip it and let the conductor clarify only if truly stuck.
# Pre-implementation spec critique (doubt-driven review): bounded + fail-open.
# Per-candidate test-command timeout for best-of-N selection (#11).
_BEST_OF_N_TEST_TIMEOUT: int = 300
# Timeout for the orchestrator-run Definition-of-Done verification oracle (B2).
# Safe, FIXED verification commands the orchestrator may auto-run as an executable
# oracle. Each command string is a literal (never built from repo content, so no
# shell-injection surface) that invokes a standard test runner; the tuple is
# (project marker file, optional substring the marker must contain, command).
# Ledger actions that mark a task's terminal state. get_task() reports a task's
# status from the most recent of these - NOT the most recent ledger entry, since
# intermediate steps (classification, each agent) are logged COMPLETED per-step
# and would otherwise make a still-running task look done.
_TERMINAL_TASK_ACTIONS: dict[str, str] = {
    "task_completed": "completed",
    "task_completed_with_failures": "completed",
    "task_failed": "failed",
    "task_cancelled": "cancelled",
    "task_stuck": "failed",
    # Not a failure of north's reasoning: no model was available to do the work.
    "task_skipped_model_unavailable": "skipped",
}

# User-facing reason for a model-scarcity skip. Kept as one literal string so the
# ledger, the SSE event, and any report all say the same honest thing.
_MODEL_SCARCITY_MESSAGE = "model pool exhausted - retry when model access recovers"


class AgentFailure(str):
    """A failed agent's name, tagged with its classified ``error_type``.

    Subclasses ``str`` (its value *is* the agent name), so it flows unchanged
    through every existing failures-list consumer - ``", ".join(...)``, ``len``,
    truthiness, equality by name. The terminal-outcome logic reads ``.error_type``
    to tell genuine failures apart from model scarcity, without re-deriving it
    from ledger history (which is racy across retries and duplicate names).
    """

    error_type: str | None

    def __new__(cls, agent_name: str, error_type: str | None = None) -> AgentFailure:
        obj = super().__new__(cls, agent_name)
        obj.error_type = error_type
        return obj


def _is_model_scarcity(failures: list[str]) -> bool:
    """True only when there are failures and *every* one was model unavailability.

    Any non-model failure makes this False, so a real bug is never mislabelled as
    a graceful skip. Plain ``str`` failures (no ``error_type``) count as non-model.
    """
    return bool(failures) and all(getattr(f, "error_type", None) == "model_unavailable" for f in failures)


def _read_artifact(path: Path | None, max_chars: int) -> str | None:
    """Read a handoff artifact file, capped; None if missing, unreadable, or empty.

    Accepts None (an agent with no declared artifact) and returns None, so callers
    on the fail-open paths never crash on a missing artifact path.
    """
    if path is None:
        return None
    try:
        if not path.is_file():
            return None
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if not text:
        return None
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n[…{len(text) - max_chars} chars truncated]"
    return text


# Checkbox task line under the spec's "## Tasks" heading, e.g. "- [ ] 1. Do X".


# Ledger status recorded for each approval-card decision. Answers to questions are
# handled separately (recorded as learnable clarifications), so they are absent here.
_APPROVAL_DECISION_STATUS: dict[str, LedgerStatus] = {
    ApprovalDecision.APPROVED: LedgerStatus.APPROVED,
    ApprovalDecision.REJECTED: LedgerStatus.REJECTED,
    ApprovalDecision.TIMEOUT_REJECTED: LedgerStatus.REJECTED,
}


class Orchestrator:
    """Coordinates the full task lifecycle across all four stages.

    Injected via ``config/dependencies.py``; never instantiated inline.
    """

    def __init__(
        self,
        ledger: LedgerWriter,
        agent_registry: AgentRegistry,
        north_star_checker: NorthStarChecker,
        execution_planner: ExecutionPlanner,
        task_context_store: TaskContextStore,
        failure_handler: FailureHandler,
        notifier: Notifier,
        stream_manager: EventStreamManager,
        approval_store: ApprovalStore,
        judgement_filter: JudgementFilter | None = None,
        north_settings: NorthSettings | None = None,
        synthesizer: ResultSynthesizer | None = None,
        tracked_router: CostTracker | None = None,
        episodic_store: Any | None = None,
        tool_registry: ToolRegistry | None = None,
        default_workspace: str = "",
        extraction_pipeline: Any | None = None,
        worktree_isolation: bool = False,
        worktree_root: str = "",
        best_of_n: int = 1,
        best_of_n_test_command: str = "",
        verify_command: str = "",
        running_task_store: RunningTaskStore | None = None,
        stuck_task_max_age_seconds: int = 86_400,
        self_repair: bool = True,
        idempotency_window_seconds: int = 60,
        critic: bool = False,
        approval_memory: ApprovalMemory | None = None,
        plan_store: Any | None = None,
    ) -> None:
        self._ledger = ledger
        # Everything that reports on a task writes through one journal: durable
        # ledger entry plus the live event, never one without the other.
        self._journal = TaskJournal(ledger=ledger, stream_manager=stream_manager)
        self._agent_registry = agent_registry
        self._north_star_checker = north_star_checker
        self._execution_planner = execution_planner
        self._task_context_store = task_context_store
        self._failure_handler = failure_handler
        self._stream_manager = stream_manager
        self._approval_store = approval_store
        self._judgement_filter = judgement_filter
        self._north_settings = north_settings
        # Single mediator for all user-facing cards (approvals, questions,
        # information). Tools and agents use the same class with their own
        # dependencies - see approval/interaction.py.
        self._interaction = UserInteraction(
            approval_store,
            notifier=notifier,
            judgement_filter=judgement_filter,
            stream_manager=stream_manager,
            on_auto_resolve=self._record_auto_resolve_ledger,
            default_timeout=(north_settings.approval_timeout_seconds if north_settings else 300.0),
        )
        self._synthesizer = synthesizer
        self._tracked_router = tracked_router
        self._episodic_store = episodic_store
        self._tool_registry = tool_registry
        # Task-scoped plan store: the conductor seeds it from the agreed spec's
        # tasks so the coder starts from (and resumes on) the agreed checklist.
        self._plan_store = plan_store
        self._default_workspace = default_workspace
        self._extraction_pipeline = extraction_pipeline
        self._worktree_isolation = worktree_isolation
        self._worktree_root = worktree_root
        self._best_of_n = max(1, best_of_n)
        self._best_of_n_test_command = best_of_n_test_command.strip()
        self._verify_command = verify_command.strip()
        self._running_task_store = running_task_store
        self._stuck_task_max_age_seconds = stuck_task_max_age_seconds
        self._self_repair = self_repair
        self._idempotency = IdempotencyCache(idempotency_window_seconds) if idempotency_window_seconds > 0 else None
        self._critic = critic
        self._approval_memory = approval_memory
        # Maps task_id → running asyncio.Task so cancel_task() can stop it.
        self._active_tasks: dict[str, asyncio.Task] = {}
        # Makes the capacity check-then-register in submit_task atomic - without
        # it, concurrent submissions could all pass the check before any of them
        # registers, bypassing MAX_CONCURRENT_TASKS.
        self._submit_lock = asyncio.Lock()
        self._last_pool_refresh_at: float = 0.0
        self._queue_wake_event = asyncio.Event()
        # Isolated / best-of-N agent execution. Given the worktree settings, a
        # place to report to, and one way to run an agent - it owns nothing else.
        self._isolation = AgentIsolation(
            enabled=worktree_isolation,
            worktree_root=worktree_root,
            best_of_n=self._best_of_n,
            test_command=best_of_n_test_command,
            stream_manager=stream_manager,
            write_ledger=self._journal.write,
            run_agent=self._run_agent_with_retry,
        )
        # Does the evidence say this is done? Runs the project's own tests and
        # scores the recorded evidence against the Definition of Done.
        self._quality_gate = QualityGate(ledger=ledger, journal=self._journal, verify_command=verify_command)
        # Does the answer match what the tools actually did? Cross-checks claims,
        # gives the agent one repair pass, and optionally runs the critic.
        self._auditor = ResultAuditor(
            journal=self._journal,
            self_repair=self_repair,
            critic=critic,
            tracked_router=tracked_router,
        )

    def notify_model_recovery(self) -> None:
        """Signal that inference models have recovered or refreshed, waking the task queue."""
        self._queue_wake_event.set()

    # ------------------------------------------------------------------ #
    #  Public API surface (called by FastAPI routes)                       #
    # ------------------------------------------------------------------ #

    async def submit_task(self, request: TaskRequest) -> TaskResponse:
        """Register and begin processing a new task. Returns immediately.

        Raises TaskCapacityError when the concurrent-task cap is reached so
        callers (API routes, webhook handler) can return 429 to the client.
        """
        async with self._submit_lock:
            if self._idempotency is not None:
                key = idempotency_key(request)
                existing_id = self._idempotency.get(key)
                if existing_id is not None:
                    existing = await self.get_task(existing_id)
                    if existing is not None and existing.status in ("pending", "running", "queued"):
                        logger.info("submit_task: deduped duplicate submission to task %s", existing_id)
                        return existing
            if len(self._active_tasks) >= MAX_CONCURRENT_TASKS:
                raise TaskCapacityError(
                    f"Too many concurrent tasks ({len(self._active_tasks)} active, "
                    f"max {MAX_CONCURRENT_TASKS}). Try again once a task finishes."
                )
            task_id = generate_task_id()
            now = utcnow()

            # Await the initial write so get_task() never returns None for a live task.
            await self._journal.write(
                LedgerEntry(
                    id=generate_id(),
                    timestamp=now,
                    source=request.source,
                    task_id=task_id,
                    input=request.prompt,
                    action="task_received",
                    status=LedgerStatus.PENDING,
                )
            )

            # Kick off async processing; store handle so cancel_task() can stop it.
            if self._running_task_store is not None:
                await self._running_task_store.mark_running(task_id, request)
            if self._idempotency is not None:
                self._idempotency.put(idempotency_key(request), task_id)
            task = asyncio.create_task(self._process_task(task_id, request))
            self._active_tasks[task_id] = task
            task.add_done_callback(lambda _: self._active_tasks.pop(task_id, None))

        return TaskResponse(
            task_id=task_id,
            status=LedgerStatus.PENDING.value,
            created_at=format_timestamp(now),
        )

    async def resume_task(self, task_id: str, request: TaskRequest, *, attempt: int = 0) -> bool:
        """Re-run an interrupted task under its original id.

        Called by the startup reconciliation sweep for tasks found in the
        RunningTaskStore (interrupted mid-flight, at any stage). Unlike submit_task
        this reuses task_id - keeping the ledger history coherent - records a
        ``task_resumed`` PENDING entry, and re-registers the task in the store with
        the given ``attempt`` so the poison-pill cap can eventually fail a task that
        keeps crashing the server.

        Returns False when the task is already in flight or the concurrency cap is
        reached; the task then stays registered and is retried on a later restart.
        """
        async with self._submit_lock:
            if task_id in self._active_tasks:
                return False
            if len(self._active_tasks) >= MAX_CONCURRENT_TASKS:
                logger.warning("resume_task: at capacity, leaving task %s registered for a later restart", task_id)
                return False

            await self._journal.write(
                LedgerEntry.new(
                    source=LedgerSource.SYSTEM,
                    task_id=task_id,
                    input=request.prompt,
                    action="task_resumed",
                    status=LedgerStatus.PENDING,
                )
            )
            if self._running_task_store is not None:
                await self._running_task_store.mark_running(task_id, request, attempt=attempt)

            task = asyncio.create_task(self._process_task(task_id, request))
            self._active_tasks[task_id] = task
            task.add_done_callback(lambda _: self._active_tasks.pop(task_id, None))

        return True

    async def drain_queued_tasks_loop(self, poll_interval: float = QUEUE_POLL_INTERVAL_SECONDS) -> None:
        """Background loop that monitors model availability and resumes queued tasks."""
        logger.info("Task queue drainer started")
        while True:
            try:
                wake_signaled = False
                with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
                    await asyncio.wait_for(self._queue_wake_event.wait(), timeout=poll_interval)
                    wake_signaled = True
                self._queue_wake_event.clear()

                if self._running_task_store is None:
                    continue

                queued_tasks = await self._running_task_store.list_queued()
                if not queued_tasks:
                    continue

                now = utcnow()
                for rt in queued_tasks:
                    if len(self._active_tasks) >= MAX_CONCURRENT_TASKS:
                        break
                    # If not explicitly woken by model recovery, enforce exponential backoff to prevent burning retries
                    if not wake_signaled and rt.attempt > 0:
                        min_delay = min(poll_interval * (2 ** max(0, rt.attempt - 1)), 60.0)
                        elapsed = (now - rt.heartbeat_at).total_seconds()
                        if elapsed < min_delay:
                            continue
                    async with self._submit_lock:
                        if rt.task_id in self._active_tasks:
                            continue
                        claimed = await self._running_task_store.mark_running_from_queued(rt.task_id)
                        if not claimed:
                            continue
                        logger.info("Resuming queued task %s (attempt %d)", rt.task_id, rt.attempt)
                        await self._journal.record(
                            rt.task_id,
                            "task_resumed",
                            status=LedgerStatus.PENDING,
                            input=rt.request.prompt,
                            payload={"attempt": rt.attempt},
                        )
                        task = asyncio.create_task(self._process_task(rt.task_id, rt.request))
                        self._active_tasks[rt.task_id] = task
                        task.add_done_callback(lambda _, tid=rt.task_id: self._active_tasks.pop(tid, None))
            except asyncio.CancelledError:
                break
            except Exception:
                logger.error("Error in task queue drainer loop", exc_info=True)
                await asyncio.sleep(poll_interval)

    async def get_task(self, task_id: str) -> TaskResponse | None:
        """Return the current status of a task.

        A task's status is its most recent *terminal* ledger entry (completed /
        failed / cancelled). Intermediate steps - classification and each agent -
        are logged COMPLETED per step, so reading the single most recent entry
        would report a still-running task as "completed" the instant it was
        classified. We scan for the terminal action instead, and report "pending",
        "queued", or "paused" while the task is still in flight.
        """
        entries = await self._ledger.query_summaries(LedgerFilters(task_id=task_id, limit=100))
        if not entries:
            return None
        # Check if currently queued or paused in running_task_store
        if self._running_task_store is not None:
            stored_task = await self._running_task_store.get(task_id)
            if stored_task is not None and stored_task.status in ("queued", "paused"):
                return TaskResponse(
                    task_id=task_id,
                    status=stored_task.status,
                    created_at=format_timestamp(entries[-1].timestamp),
                )

        for entry in entries:  # query() returns most-recent-first
            terminal = _TERMINAL_TASK_ACTIONS.get(entry.action)
            if terminal is not None:
                return TaskResponse(task_id=task_id, status=terminal, created_at=format_timestamp(entry.timestamp))
            if entry.action == "task_queued":
                return TaskResponse(task_id=task_id, status="queued", created_at=format_timestamp(entry.timestamp))
            if entry.action == "task_paused":
                return TaskResponse(task_id=task_id, status="paused", created_at=format_timestamp(entry.timestamp))
        # No terminal entry yet - the task is still running.
        return TaskResponse(task_id=task_id, status="pending", created_at=format_timestamp(entries[-1].timestamp))

    async def cancel_task(self, task_id: str) -> bool:
        """Cancel a running task: stop its pipeline and write a terminal ledger entry.

        Returns False when the task is not in flight (unknown id or already
        finished) - writing a CANCELLED entry then would rewrite the history
        of a completed task, since get_task() reads the most recent entry.
        """
        running = self._active_tasks.pop(task_id, None)
        was_stored = False
        if running is None and self._running_task_store is not None:
            stored = await self._running_task_store.get(task_id)
            was_stored = stored is not None
        if running is None and not was_stored:
            return False
        if running is not None and not running.done():
            running.cancel()
        if self._tracked_router:
            self._tracked_router.pop_task_cost(task_id)
        # A user-cancelled task must not be recovered on restart, and its
        # _process_task finally cannot reliably clear the registry once the task
        # is being cancelled (awaits in a cancelling task re-raise CancelledError),
        # so drop it here explicitly. Shutdown/crash deliberately leave the row so
        # the task is resumed next start.
        if self._running_task_store is not None:
            await self._running_task_store.clear(task_id)
        await self._journal.record(task_id, "task_cancelled", status=LedgerStatus.CANCELLED)
        await self._stream_manager.emit_done(task_id)
        return True

    async def cancel_all_tasks(self) -> int:
        """Cancel every in-flight task. Returns the count cancelled.

        Snapshots the ids first because ``cancel_task`` mutates ``_active_tasks``.
        """
        cancelled = 0
        for task_id in list(self._active_tasks.keys()):
            if await self.cancel_task(task_id):
                cancelled += 1
        return cancelled

    async def pause_task(self, task_id: str) -> bool:
        """Pause a running task: cancel its async pipeline but keep it resumable.

        Unlike cancel_task, the task is NOT cleared from the running-task store
        and gets a ``task_paused`` ledger entry instead of ``task_cancelled``.
        The task can later be re-run via resume_paused_task().
        """
        running = self._active_tasks.pop(task_id, None)
        if running is None:
            return False
        # Keep the row in the store but mark it paused so reconcile won't
        # auto-resume it — only an explicit resume_paused_task() call should.
        # Persist this state before cancellation so _process_task's finally block
        # can never race ahead and delete the recovery row.
        if self._running_task_store is not None:
            await self._running_task_store.mark_paused(task_id)
        if not running.done():
            running.cancel()
        if self._tracked_router:
            self._tracked_router.pop_task_cost(task_id)
        await self._journal.record(task_id, "task_paused", status=LedgerStatus.PENDING)
        await self._stream_manager.emit_done(task_id)
        return True

    async def resume_paused_task(self, task_id: str) -> bool:
        """Re-run a paused task under its original id.

        The task must exist in the running-task store with status='paused'.
        Returns False if the task is not paused or already in flight.
        """
        async with self._submit_lock:
            if task_id in self._active_tasks:
                return False
            if self._running_task_store is None:
                return False
            if not await self._running_task_store.mark_running_from_paused(task_id):
                return False
            # Reconstruct the request from the store.
            rt = await self._running_task_store.get(task_id)
            if rt is None:
                return False
            if len(self._active_tasks) >= MAX_CONCURRENT_TASKS:
                logger.warning("resume_paused_task: at capacity, leaving task %s paused", task_id)
                await self._running_task_store.mark_paused(task_id)
                return False
            await self._journal.write(
                LedgerEntry.new(
                    source=LedgerSource.SYSTEM,
                    task_id=task_id,
                    input=rt.request.prompt,
                    action="task_resumed",
                    status=LedgerStatus.PENDING,
                )
            )
            task = asyncio.create_task(self._process_task(task_id, rt.request))
            self._active_tasks[task_id] = task
            task.add_done_callback(lambda _: self._active_tasks.pop(task_id, None))
        return True

    async def respond_approval(
        self,
        card_id: str,
        decision: str,
        chosen_option: str,
    ) -> None:
        """Record a user approval decision from the notification callback or Web UI.

        The decision is bound to the server-issued card: task_id and agent are
        taken from the stored card, never from the client, and a card can only
        be resolved while it is pending.

        Raises:
            LookupError: card_id does not correspond to an issued card.
            ValueError: the card was already resolved.
        """
        card = self._approval_store.get(card_id)
        if card is None:
            raise LookupError(f"Unknown approval card {card_id!r}.")
        if card.status != "pending":
            raise ValueError(f"Approval card {card_id!r} is already resolved ({card.status}).")

        if not self._approval_store.resolve(card_id, decision, chosen_option=chosen_option):
            raise ValueError(f"Approval card {card_id!r} could not be resolved.")

        # An answered question is a durable preference in the user's own words  -
        # record it from a *learnable* source (the extraction pipeline reads it),
        # phrased so the fact comes from the answer, not north's question. Every
        # other decision is an audit-only APPROVAL entry.
        is_clarification = (
            card.type == CardType.QUESTION and decision == ApprovalDecision.ANSWERED and bool(chosen_option.strip())
        )
        if is_clarification:
            source = LedgerSource.CLARIFICATION
            status = LedgerStatus.COMPLETED
            action = "clarification_answered"
            ledger_input = f"north asked: {card.message}\nThe user answered: {chosen_option}"
        else:
            source = LedgerSource.APPROVAL
            status = _APPROVAL_DECISION_STATUS.get(decision, LedgerStatus.REJECTED)
            action = f"approval_responded: {decision}"
            ledger_input = (
                f"question: {card.message}\noptions: {', '.join(card.options)}"
                if card.message
                else f"card_id={card_id}"
            )
            # Learn from this human decision so autonomous mode can replay it later.
            if self._approval_memory is not None and card.type == CardType.APPROVAL:
                self._approval_memory.record(card.agent, card.message, decision)
        await self._journal.record(
            card.task_id,
            action,
            status=status,
            source=source,
            agent=card.agent,
            input=ledger_input,
            output=f"chosen_option={chosen_option or decision}",
            event="approval_responded",
            payload={"card_id": card_id, "decision": decision, "chosen_option": chosen_option},
        )

    async def emit_steer(self, task_id: str, instruction: str) -> None:
        """Publish an in-flight steering directive to an active task."""
        await self._stream_manager.emit(
            task_id,
            "task_steered",
            {"task_id": task_id, "instruction": instruction, "timestamp": format_timestamp(utcnow())},
        )
        await self._journal.write(
            LedgerEntry.new(
                source=LedgerSource.CLARIFICATION,
                task_id=task_id,
                agent="user",
                action="task_steered",
                output=instruction,
                status=LedgerStatus.COMPLETED,
            )
        )

    @property
    def active_task_ids(self) -> frozenset[str]:
        """Ids of tasks currently in flight (asyncio tasks still running)."""
        return frozenset(self._active_tasks)

    @property
    def running_task_store(self) -> RunningTaskStore | None:
        """The durable in-flight registry (used by reconciliation and the watchdog)."""
        return self._running_task_store

    @property
    def stuck_task_max_age_seconds(self) -> int:
        """Age past which an interrupted/stalled task is failed rather than resumed."""
        return self._stuck_task_max_age_seconds

    async def cancel_stuck_task(self, task_id: str) -> bool:
        """Fail a task the watchdog found stalled: record why, then cancel it."""
        running = self._active_tasks.pop(task_id, None)
        was_stored = False
        if running is None and self._running_task_store is not None:
            stored = await self._running_task_store.get(task_id)
            was_stored = stored is not None
        if running is None and not was_stored:
            return False
        if running is not None and not running.done():
            running.cancel()
        if self._tracked_router:
            self._tracked_router.pop_task_cost(task_id)
        if self._running_task_store is not None:
            await self._running_task_store.clear(task_id)

        await self._journal.record(
            task_id,
            "task_stuck",
            status=LedgerStatus.FAILED,
            output=f"No progress for over {self._stuck_task_max_age_seconds}s - cancelling as stuck (watchdog).",
            error_type="stuck_timeout",
            payload={"error": "stuck_timeout"},
        )
        await self._stream_manager.emit_done(task_id)
        return True

    async def list_active_tasks(self) -> list[TaskResponse]:
        """Returns tasks that are currently in-flight (active, queued, or paused)."""
        task_ids = set(self._active_tasks)
        if self._running_task_store is not None:
            for rt in await self._running_task_store.list_all():
                task_ids.add(rt.task_id)
        responses = await asyncio.gather(*(self.get_task(task_id) for task_id in task_ids))
        return [resp for resp in responses if resp is not None]

    # ------------------------------------------------------------------ #
    #  Internal helpers                                                    #
    # ------------------------------------------------------------------ #

    def _release_pending_cards(self, task_id: str) -> None:
        """Resolve any approval/question cards this finished task left pending.

        Best-effort and synchronous, so it is safe to call while a cancellation
        is unwinding. Anything waiting on one of these cards wakes immediately
        with a `task_ended` status rather than sitting until its timeout.

        Nothing here may raise: this runs in ``_process_task``'s ``finally``, and
        at shutdown the loop can already be closed - so a failed notification
        would replace whatever actually ended the task with a confusing
        "event loop is closed". Resolving the cards is the part that matters;
        telling the UI is a courtesy.
        """
        released: list[Card] = []
        try:
            released = self._approval_store.cancel_for_task(task_id)
            for card in released:
                spawn(
                    self._stream_manager.emit(
                        task_id,
                        "approval_responded",
                        {"card_id": card.id, "decision": card.status, "chosen_option": ""},
                    ),
                    name="approval_card_released",
                )
        except Exception:
            logger.debug(
                "releasing pending cards for task %s: %d resolved, notification failed",
                task_id,
                len(released),
                exc_info=True,
            )

    async def _heartbeat(self, task_id: str) -> None:
        """Signal that *task_id* is making progress so the watchdog leaves it alone."""
        if self._running_task_store is None:
            return
        try:
            await self._running_task_store.heartbeat(task_id)
        except Exception:
            logger.debug("running-task heartbeat failed for %s", task_id, exc_info=True)

    # ------------------------------------------------------------------ #
    #  Notification with judgement filtering                               #
    # ------------------------------------------------------------------ #

    async def _record_auto_resolve_ledger(self, card: Card, decision: str, chosen_option: str) -> None:
        """Audit hook for UserInteraction: log a JudgementFilter auto-decision.

        Writes the same APPROVAL ledger entry a user decision would, so an
        auto-approved/rejected card stays fully traceable.
        """
        await self._journal.write(
            LedgerEntry.new(
                source=LedgerSource.APPROVAL,
                task_id=card.task_id,
                agent=card.agent,
                action=f"judgement_filter_auto_{decision}",
                input=card.title,
                output=chosen_option or decision,
                status=LedgerStatus.COMPLETED,
            )
        )

    # ------------------------------------------------------------------ #
    #  Stage pipeline                                                      #
    # ------------------------------------------------------------------ #

    def _detect_strategy_command(self, prompt: str) -> StrategyMode | None:
        """Return a StrategyMode if the prompt is an unambiguous strategy command.

        Requires the prompt to be *only* a strategy directive - no surrounding
        prose - so incidental mentions ("I was in sport mode") never mutate
        the running strategy.
        """
        match = STRATEGY_CMD_RE.match(prompt.strip())
        if match:
            return StrategyMode(match.group(1).lower())
        return None

    async def _handle_strategy_command(self, task_id: str, prompt: str) -> bool:
        """Process strategy commands and return True if handled."""
        if self._north_settings is None:
            return False

        mode = self._detect_strategy_command(prompt)
        if mode is None:
            return False

        self._north_settings.set_power(mode)
        msg = f"Strategy set to **{mode.value}**. {describe(mode)}"
        await self._journal.record(
            task_id, "agent_completed", agent="orchestrator", output=msg, event="task_completed"
        )
        await self._stream_manager.emit_done(task_id)
        return True

    async def _process_task(self, task_id: str, request: TaskRequest) -> None:
        """Full pipeline: classify → north-star → route → execute."""
        bind_task_id(task_id)  # attach correlation ID to every log line in this context
        task_start = time.monotonic()
        try:
            # Strategy command shortcut - handle before full pipeline
            if await self._handle_strategy_command(task_id, request.prompt):
                return

            # Manual agent trigger (`north agent run <name>`): run exactly that
            # agent, bypassing classification and the planner so it is never re-routed.
            if request.forced_agent:
                await self._run_forced_agent(task_id, request)
                return

            classification, plan = await self._stage_plan(task_id, request.prompt, request.context)
            # Stamp the domain on the running_task row so other agents
            # can see which domain this session belongs to.
            if self._running_task_store is not None:
                await self._running_task_store.update_domain(task_id, classification.domain)
            await self._stage_north_star(task_id, request.prompt, classification)
            await self._stage_execute(
                task_id,
                request.prompt,
                plan,
                request.workspace,
                domain=classification.domain,
                context=request.context,
                confidence=classification.confidence,
            )
        except asyncio.CancelledError:
            # cancel_task() already wrote the ledger entry and emitted events.
            raise
        except NorthStarConflictError as e:
            logger.warning("Task %s rejected: conflicts with North Star goals", task_id)
            with contextlib.suppress(Exception):
                await self._task_context_store.update_task_status(task_id, "failed")
            await self._stream_manager.emit(task_id, "task_rejected", {"reason": str(e)})
            await self._record_task_failure(task_id, task_start, str(e), LedgerStatus.CANCELLED, "north_star_conflict")
            await self._stream_manager.emit_done(task_id)
        except Exception as e:
            error_type = classify_error(e)
            with contextlib.suppress(Exception):
                await self._task_context_store.update_task_status(task_id, "failed")
            if error_type == "model_unavailable":
                # Check attempt count to allow queueing / retry on model recovery
                current_attempt = 0
                if self._running_task_store is not None:
                    stored_task = await self._running_task_store.get(task_id)
                    if stored_task is not None:
                        current_attempt = stored_task.attempt
                if current_attempt < MAX_QUEUE_ATTEMPTS:
                    logger.warning(
                        "Task %s queued for model availability recovery (attempt %d): %s",
                        task_id,
                        current_attempt + 1,
                        e,
                    )
                    if self._running_task_store is not None:
                        await self._running_task_store.mark_queued(task_id, attempt=current_attempt + 1)
                    await self._journal.record(
                        task_id,
                        "task_queued",
                        status=LedgerStatus.PENDING,
                        output=f"model pool unavailable - queued for retry (attempt {current_attempt + 1})",
                        payload={
                            "reason": "model pool unavailable - queued for retry",
                            "attempt": current_attempt + 1,
                        },
                    )
                    self._queue_wake_event.set()
                    return

                # Not a north failure: the whole model pool was unavailable, so the
                # work could not proceed. Skip honestly (autonomous mode just moves
                # on) rather than reporting a failure the user would read as a bug.
                logger.warning("Task %s skipped - %s: %s", task_id, _MODEL_SCARCITY_MESSAGE, e)
                await self._stream_manager.emit(
                    task_id, "task_skipped", {"reason": _MODEL_SCARCITY_MESSAGE, "error_type": error_type}
                )
                await self._record_task_skipped_model_unavailable(task_id, task_start)
            else:
                logger.error("Task %s failed: %s", task_id, e, exc_info=True)
                await self._stream_manager.emit(task_id, "task_failed", {"error": str(e), "error_type": error_type})
                await self._record_task_failure(task_id, task_start, str(e), LedgerStatus.FAILED, error_type)
            await self._stream_manager.emit_done(task_id)
        finally:
            # Reap this task's tracked cost exactly once, regardless of exit path.
            # The success/failure/conflict/cancel paths already pop it to record
            # the cost in the ledger; popping again here is a no-op (pop_task_cost
            # returns 0.0 when absent), so this only catches tasks that recorded
            # cost but never reached one of those pops - preventing an unbounded
            # leak in CostTracker._task_costs on a long-lived server.
            if self._tracked_router is not None:
                self._tracked_router.pop_task_cost(task_id)
            # This task can no longer act on an answer, so stop asking for one.
            # Left pending, its cards would keep appearing in the approvals list
            # and would never be evicted (the cap spares pending cards), so a
            # cancelled or failed task leaked a card and an event apiece.
            # Synchronous on purpose: this runs in a `finally` that may already
            # be unwinding a cancellation, where an await would re-raise.
            self._release_pending_cards(task_id)
            if self._plan_store is not None and hasattr(self._plan_store, "clear"):
                self._plan_store.clear(task_id)
            if self._failure_handler is not None and hasattr(self._failure_handler, "clear_all"):
                self._failure_handler.clear_all(task_id)
            # The task has reached a terminal state (success/failure/cancel/skip), so it
            # is no longer in-flight: drop it from the crash-recovery registry unless queued.
            if self._running_task_store is not None:
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    stored_task = await asyncio.shield(self._running_task_store.get(task_id))
                    is_resumable = stored_task is not None and stored_task.status in {"queued", "paused"}
                    if not is_resumable:
                        await asyncio.shield(self._running_task_store.clear(task_id))

    async def _stage_plan(
        self, task_id: str, prompt: str, context: str = ""
    ) -> tuple[IntentClassification, ExecutionPlan]:
        """Stages 1+3: Classify intent and build execution plan in one LLM call."""
        await self._stream_manager.emit(task_id, "classifying", {"prompt": prompt})

        classification, plan = await self._execution_planner.plan_all(prompt, task_id=task_id, context=context)

        await self._journal.record(
            task_id,
            f"classified_as_{'consequential' if classification.is_consequential else 'trivial'}",
            output=classification.reasoning,
            # Stamp the domain so the episode consolidator can tag each task's
            # episode without re-deriving it (used for per-agent gating).
            agent_output={"domain": classification.domain},
            event="classified",
            payload={
                "is_consequential": classification.is_consequential,
                "domain": classification.domain,
                "reasoning": classification.reasoning,
            },
        )
        await self._stream_manager.emit(
            task_id,
            "routed",
            {
                "agents": plan.agents,
                "parallel_groups": plan.parallel_groups,
                "mode": plan.mode.value,
            },
        )
        await self._task_context_store.initialize_task(task_id, plan.agents)
        return classification, plan

    def _resolve_task_model_pool(
        self, plan: ExecutionPlan | None = None, domain: str = "", confidence: float = 1.0
    ) -> str:
        """The model pool for this task's agent runs - see `orchestrator/tiering.py`.

        The power dial is deliberately not applied here: `ModelDispatcher` already
        forces the pool for eco and sport when the request is dispatched, so
        handling it twice would just be two places to disagree.
        """
        return resolve_model_pool(plan, domain, confidence)

    async def _run_forced_agent(self, task_id: str, request: TaskRequest) -> None:
        """Run a single named agent directly, bypassing classification and the planner.

        Backs `north agent run <name>`: a manual, explicit agent trigger runs exactly
        that agent's ReAct loop - it is not re-routed by the planner. The name is
        validated at the API boundary, so the agent is known-registered here.
        """
        agent = self._agent_registry.get(request.forced_agent)
        workspace = request.workspace or self._default_workspace
        model_pool = self._resolve_task_model_pool(domain=agent.domain)
        await self._task_context_store.initialize_task(task_id, [agent.name])
        await self._stream_manager.emit(task_id, "executing", {"agents": [agent.name]})
        failures = await self._execute_agent_group(
            task_id,
            request.prompt,
            [agent],
            workspace,
            context=request.context,
            model_pool=model_pool,
        )
        if failures:
            await self._report_execution_failures(task_id, failures)
        await self._finish_task(task_id, failures=failures, total_agents=1)

    async def _handle_alignment_conflict(self, task_id: str, tension: str) -> None:
        """Prompt user for approval when a North Star conflict is detected.

        The north_star_conflict SSE event is emitted first for UI awareness, then
        the card is routed through the shared UserInteraction mediator so the
        JudgementFilter and Notifier are applied consistently with every other
        approval card.
        """
        card = Card(
            id=generate_id(),
            type=CardType.APPROVAL,
            task_id=task_id,
            agent="orchestrator",
            title="North Star Conflict Detected",
            message=tension or "This task conflicts with one of your active goals. Proceed?",
            options=["Proceed anyway", "Cancel"],
        )
        # Emit the conflict event before surfacing so the UI can show context.
        await self._stream_manager.emit(task_id, "north_star_conflict", {"tension": tension})
        # The shared mediator registers the card, applies the JudgementFilter
        # (auto-resolving if a rule matches), fires the Notifier, and blocks until
        # the user responds or the timeout elapses (then TIMEOUT_REJECTED).
        timeout = self._north_settings.approval_timeout_seconds if self._north_settings else 300.0
        decided = await self._interaction.request_decision(card, timeout=timeout)
        if decided.status == ApprovalDecision.TIMEOUT_REJECTED:
            logger.warning("North Star approval timed out for task %s - treating as rejection", task_id)
        if decided.status != ApprovalDecision.APPROVED:
            raise NorthStarConflictError(tension or "North Star conflict")

    async def _stage_north_star(
        self,
        task_id: str,
        prompt: str,
        classification: IntentClassification,
    ) -> None:
        """Stage 2: North Star alignment check (consequential tasks only)."""
        if not classification.is_consequential:
            return

        # Skip when the classifier is uncertain to avoid false interruptions on
        # borderline tasks (e.g. "schedule a reminder" - local? external?).
        if classification.confidence < NORTH_STAR_CONFIDENCE_THRESHOLD:
            logger.info(
                "Skipping north star check for task %s - classifier confidence %.2f < %.2f threshold",
                task_id,
                classification.confidence,
                NORTH_STAR_CONFIDENCE_THRESHOLD,
            )
            await self._stream_manager.emit(
                task_id,
                "north_star_aligned",
                {"reasoning": "skipped: low-confidence consequential classification"},
            )
            return

        await self._stream_manager.emit(task_id, "north_star_checking", {})
        try:
            aligned, tension, reasoning = await self._north_star_checker.check_alignment(prompt, task_id=task_id)
        except OrchestratorError as e:
            # Fail open, visibly. This error means the check could not be *run* - a
            # malformed JSON reply, an unreachable model - not that a conflict was
            # found. Blocking on it made a flaky cheap model able to kill any
            # consequential task, and a harness fault is far likelier than a real
            # goal conflict. The task proceeds; the gap is recorded and surfaced so
            # the reader knows this run was not checked against their goals.
            logger.warning("North Star check could not run - proceeding unchecked: %s", e)
            await self._journal.record(
                task_id,
                "north_star_check_failed",
                output=f"Goal alignment was not verified for this task: {e}",
                error_type="north_star_check_unavailable",
                payload={"reason": str(e)},
            )
            return

        check_action = "north_star_check_aligned" if aligned else "north_star_check_conflict"
        await self._journal.write(
            LedgerEntry.new(
                source=LedgerSource.SYSTEM,
                task_id=task_id,
                action=check_action,
                output=reasoning,
                status=LedgerStatus.COMPLETED,
            )
        )

        if not aligned:
            await self._handle_alignment_conflict(task_id, tension or "Goal conflict detected")

        await self._stream_manager.emit(task_id, "north_star_aligned", {"reasoning": reasoning})

    async def _skip_agent_with_failed_deps(self, task_id: str, name: str, failed_deps: list[str]) -> None:
        """Record an agent as skipped because its dependencies failed."""
        logger.warning(
            "Skipping agent '%s' in task %s - dependencies failed: %s",
            name,
            task_id,
            failed_deps,
        )
        # Mark failed in the task context so any read() waiting on this agent's
        # output errors out immediately instead of blocking until timeout.
        await self._task_context_store.update_agent_status(task_id, name, "failed")
        await self._stream_manager.emit(task_id, "agent_skipped", {"agent": name, "failed_dependencies": failed_deps})
        await self._journal.write(
            LedgerEntry.new(
                source=LedgerSource.AGENT,
                task_id=task_id,
                agent=name,
                action="agent_skipped",
                output=f"Skipped - dependencies failed: {', '.join(failed_deps)}",
                status=LedgerStatus.FAILED,
                error_type="dependency_failure",
            )
        )

    def _use_conductor(self, domain: str, plan: ExecutionPlan) -> bool:
        """True when this task runs the code IMPLEMENT + VERIFY phases (the conductor).

        Applies to any engineering task that actually involves writing code
        (``coder`` in ``plan.agents``). The no-code kinds - ``question`` (researcher
        only) and ``research`` (researcher → architect) - are read-only and are NOT
        forced through the coder + Definition-of-Done gate. ``deploy``/``ship`` is a
        separate flow (SHIP phase), not a coding loop.
        """
        if domain != "engineering":
            return False
        if plan.engineering_kind in DEPLOY_KINDS:
            return False  # deploy/ship is a distinct human-gated flow, not a coding loop
        if "coder" not in plan.agents:
            return False
        return {"coder", "reviewer"} <= set(self._agent_registry.names())

    def _use_deploy_flow(self, domain: str, plan: ExecutionPlan) -> bool:
        """True when this is an engineering deploy/ship task and the coder is available."""
        return (
            domain == "engineering"
            and plan.engineering_kind in DEPLOY_KINDS
            and "coder" in set(self._agent_registry.names())
        )

    async def _run_deploy_flow(
        self, task_id: str, prompt: str, workspace: str, context: str = "", model_pool: str = "reasoning"
    ) -> list[str]:
        """Ship already-completed work: a single git/gh-capable agent, human-gated.

        Deploy is deliberately NOT the conductor and NOT gated by the code DoD - there
        is no new code to review or verify. The agent runs with a shipping framing that
        requires a semantic approval checkpoint before any external side effect (push /
        PR) and a second explicit approval before any merge or production deploy.
        """
        coder = self._agent_registry.get("coder")
        deploy_prompt = f"{DEPLOY_PREAMBLE}\n\n{prompt}"
        return await self._execute_agent_group(
            task_id, deploy_prompt, [coder], workspace, context=context, model_pool=model_pool
        )

    def _human_available(self) -> bool:
        """True when a human can be asked (any mode except autonomous)."""
        if self._north_settings is None:
            return True
        return resolve_approval_mode(self._north_settings) != ApprovalMode.AUTONOMOUS

    def _use_design_phase(self, plan: ExecutionPlan) -> bool:
        """True when a task should get the interactive clarify+design phase first.

        Only for larger code kinds (feature/refactor) and only when a human is
        available to discuss - autonomous runs skip it and let the conductor design
        from the user's known preferences + best practices. Requires researcher +
        architect registered.
        """
        return (
            plan.engineering_kind in DESIGN_KINDS
            and self._human_available()
            and {"researcher", "architect"} <= set(self._agent_registry.names())
        )

    async def _run_design_phase(
        self, task_id: str, prompt: str, workspace: str, context: str = "", model_pool: str = "reasoning"
    ) -> list[str]:
        """Interactive clarify + design: researcher gathers context (clarifying scope
        with the user if unclear), then the architect proposes and DISCUSSES a solution
        with the user until aligned, writing the agreed spec. Returns failures (empty
        on success). The conductor then implements the agreed spec."""
        researcher = self._agent_registry.get("researcher")
        architect = self._agent_registry.get("architect")
        await self._stream_manager.emit(task_id, "design_phase", {"step": "research"})
        r_fail = await self._execute_agent_group(
            task_id,
            f"{DESIGN_RESEARCH_PREAMBLE}\n\n{prompt}",
            [researcher],
            workspace,
            context=context,
            model_pool=model_pool,
        )
        if r_fail:
            return r_fail
        research = await asyncio.to_thread(
            _read_artifact, self._primary_artifact_path("researcher", task_id), _HANDOFF_ARTIFACT_MAX_CHARS
        )
        design_ctx = f"{context}\n\n## Research context\n{research}" if research else context
        await self._stream_manager.emit(task_id, "design_phase", {"step": "design"})
        a_fail = await self._execute_agent_group(
            task_id,
            f"{DESIGN_ARCHITECT_PREAMBLE}\n\n{prompt}",
            [architect],
            workspace,
            context=design_ctx,
            model_pool=model_pool,
        )
        if a_fail:
            return a_fail
        # A "successful" architect that wrote no usable spec must not send the coder to
        # implement a phantom file - treat a missing/trivial spec as a design failure.
        spec = await asyncio.to_thread(_read_artifact, Path(self._spec_path(task_id)), _HANDOFF_ARTIFACT_MAX_CHARS)
        if not spec or len(spec.strip()) < SPEC_MIN_CHARS:
            await self._warn_missing_handoff_artifact(task_id, "architect")
            return ["architect"]
        return []

    def _spec_path(self, task_id: str) -> str:
        """Path to the architect's agreed design spec for this task."""
        return f"{handoff_dir_for(task_id)}/architecture/spec.md"

    def _coder_preamble_for_kind(self, kind: str) -> str:
        """The coder's framing for a code kind (debug = reproduce-first, test =
        tests-only); the default principal-engineer framing for anything else."""
        return CONDUCTOR_CODER_PREAMBLES.get(kind.strip().lower(), CONDUCTOR_CODER_PREAMBLE)

    def _coder_preamble_for_agreed_spec(self, task_id: str, critique: list[str] | None = None) -> str:
        """The coder's framing when a design was agreed with the user: implement that
        spec as-is rather than redesign. Any pre-implementation critique concerns are
        appended as a bounded, within-spec checklist (never a licence to redesign)."""
        preamble = CONDUCTOR_CODER_PREAMBLE_SPEC.format(spec_path=self._spec_path(task_id))
        if critique:
            issues = "\n".join(f"- {concern}" for concern in critique)
            preamble += SPEC_CRITIQUE_INJECTION.format(issues=issues)
        return preamble

    async def _seed_plan_from_spec(self, task_id: str) -> int:
        """Seed the plan store from the agreed spec's ``## Tasks`` checklist.

        The coder then starts from - and resumes on - the agreed checklist (north's
        STATE equivalent, via the existing plan_store) rather than re-deriving it.
        Returns the number of tasks seeded; 0 when unavailable or none parse.
        """
        if self._plan_store is None:
            return 0
        spec = await asyncio.to_thread(_read_artifact, Path(self._spec_path(task_id)), _HANDOFF_ARTIFACT_MAX_CHARS)
        if not spec:
            return 0
        tasks = parse_spec_tasks(spec)
        if not tasks:
            return 0
        self._plan_store.set_plan(task_id, [{"content": task, "status": "pending"} for task in tasks])
        await self._stream_manager.emit(task_id, "plan_seeded", {"tasks": len(tasks)})
        return len(tasks)

    async def _rubber_duck_spec(self, task_id: str, prompt: str, workspace: str) -> list[str]:
        """Independent, fresh-context critique of the agreed spec before implementation.

        A one-shot, timeout-bounded, fail-open review that runs on a DIFFERENT model
        than the architect (a genuine second opinion): it surfaces concrete flaws for
        the coder to resolve, never blocks the pipeline, and records whether it was
        truly independent. Returns the concerns (empty on skip/error).
        """
        if self._tracked_router is None:
            return []
        spec = await asyncio.to_thread(_read_artifact, Path(self._spec_path(task_id)), _HANDOFF_ARTIFACT_MAX_CHARS)
        if not spec or len(spec.strip()) < SPEC_MIN_CHARS:
            return []  # too little to critique; a truly absent spec is caught in _run_design_phase
        research = await asyncio.to_thread(
            _read_artifact, self._primary_artifact_path("researcher", task_id), _HANDOFF_ARTIFACT_MAX_CHARS
        )
        exclude = await self._models_used_by(task_id, {"architect"})
        critique_prompt = SPEC_CRITIQUE_PROMPT.format(
            prompt=prompt[:1500], research=(research or "(none)")[:2000], spec=spec[:_HANDOFF_ARTIFACT_MAX_CHARS]
        )
        try:
            response = await asyncio.wait_for(
                self._tracked_router.complete(
                    CompletionRequest(
                        prompt=critique_prompt,
                        priority=PoolPriority.MEDIUM,
                        component="spec_critique",
                        task_id=task_id,
                        json_mode=True,
                        max_tokens=800,
                        temperature=0.2,
                        exclude_models=exclude,
                    )
                ),
                timeout=SPEC_CRITIQUE_TIMEOUT_S,
            )
            verdict = extract_json(response.text)
        except Exception:
            logger.debug("spec critique skipped (error/timeout) for task %s", task_id, exc_info=True)
            return []
        issues = clean_issues(verdict.get("issues") if isinstance(verdict, dict) else None)
        independent = bool(exclude) and response.model_used not in exclude
        await self._journal.record(
            task_id,
            "spec_critique",
            agent="spec_critic",
            output="; ".join(issues) if issues else "spec sound",
            agent_output={
                "model_used": response.model_used,
                "independent": independent,
                "issue_count": len(issues),
            },
            payload={"issues": issues, "model": response.model_used, "independent": independent},
        )
        return issues

    async def _run_engineering_conductor(
        self,
        task_id: str,
        prompt: str,
        workspace: str,
        coder_preamble: str,
        context: str = "",
        model_pool: str = "reasoning",
    ) -> list[str]:
        """The IMPLEMENT + VERIFY phase: one continuous coder (framed by
        ``coder_preamble``), then an independent different-model reviewer with a
        bounded coder-fix loop.

        The orchestrator (not the model) deterministically runs the reviewer, reads
        its structured verdict, and sends the coder back once per must-fix round up
        to a cap. The Definition-of-Done gate (evaluated after this returns) is the
        final backstop.
        """
        coder = self._agent_registry.get("coder")
        reviewer = self._agent_registry.get("reviewer")

        coder_prompt = f"{coder_preamble}\n\n{prompt}"
        failures = await self._execute_agent_group(
            task_id, coder_prompt, [coder], workspace, context=context, model_pool=model_pool
        )
        if failures:
            return failures  # coder failed - nothing to review

        review_prompt = f"{prompt}\n\n{CONDUCTOR_REVIEW_PROMPT}"
        for fix_round in range(CONDUCTOR_MAX_FIX_ROUNDS + 1):
            review_failures = await self._execute_agent_group(
                task_id,
                review_prompt,
                [reviewer],
                workspace,
                context=context,
                allow_delegation=False,
                model_pool=model_pool,
            )
            if review_failures:
                if _is_model_scarcity(review_failures):
                    # The coder's work stands; only the *independent* review could
                    # not get a model. Don't sink a task whose real work is done -
                    # skip the review honestly and let the deterministic DoD gate
                    # (which flags a missing verdict) mark the reduced rigor. north
                    # keeps its bar; it just reports the review was skipped for
                    # model scarcity, so the task ends completed-with-failures.
                    await self._stream_manager.emit(
                        task_id, "conductor_review_skipped_model_unavailable", {"reason": _MODEL_SCARCITY_MESSAGE}
                    )
                    await self._journal.write(
                        LedgerEntry.new(
                            source=LedgerSource.SYSTEM,
                            task_id=task_id,
                            agent="reviewer",
                            action="review_skipped_model_unavailable",
                            output=f"Independent review skipped: {_MODEL_SCARCITY_MESSAGE}",
                            status=LedgerStatus.COMPLETED,
                            error_type="model_unavailable",
                        )
                    )
                    return []
                return review_failures  # a genuine reviewer failure - unchanged

            review = read_review_result(task_id)
            if review is not None and review.passed:
                return []  # verified pass - done

            if review is None:
                # The reviewer did not emit the required structured verdict. Do NOT
                # treat that as done - retry the reviewer (demanding the JSON) if
                # budget remains; otherwise stop and let the DoD gate flag the
                # unverified result honestly.
                await self._stream_manager.emit(task_id, "conductor_review_missing_verdict", {})
                if fix_round >= CONDUCTOR_MAX_FIX_ROUNDS:
                    return []
                review_prompt = f"{prompt}\n\n{CONDUCTOR_REVIEW_RETRY_PROMPT}"
                continue  # re-run the reviewer; nothing structured for the coder to fix yet

            # review present and FAILED with must-fix items.
            if fix_round >= CONDUCTOR_MAX_FIX_ROUNDS:
                await self._stream_manager.emit(task_id, "conductor_review_unresolved", {"must_fix": review.must_fix})
                return []

            items = "\n".join(f"- {m}" for m in review.must_fix) or "(see the review report)"
            await self._stream_manager.emit(task_id, "conductor_fix_round", {"round": fix_round + 1})
            fix_prompt = f"{prompt}\n\n{CONDUCTOR_FIX_PREAMBLE.format(items=items[:_HANDOFF_ARTIFACT_MAX_CHARS])}"
            fix_failures = await self._execute_agent_group(
                task_id, fix_prompt, [coder], workspace, context=context, model_pool=model_pool
            )
            if fix_failures:
                return fix_failures  # coder fix failed
        return []

    async def _execute_hierarchical_groups(
        self,
        task_id: str,
        prompt: str,
        plan: ExecutionPlan,
        workspace: str,
        context: str = "",
        model_pool: str = "reasoning",
    ) -> list[str]:
        """Execute agents in hierarchical mode, passing results from earlier steps.

        Agents whose declared dependencies already failed are skipped (and
        counted as failures) rather than run with missing upstream context.
        Agents without declared dependencies always run. After each stage the
        actual handoff artifacts it produced (spec.md, context.md, ...) are read
        and injected verbatim into downstream context, and any artifact a stage
        was expected to produce but didn't is surfaced (#3 enforced handoff).
        """
        all_failures: list[str] = []
        accumulated_snippets: list[str] = []
        for group in plan.parallel_groups:
            runnable: list[str] = []
            for name in group:
                failed_deps = [d for d in plan.dependencies.get(name, []) if d in all_failures]
                if failed_deps:
                    all_failures.append(name)
                    await self._skip_agent_with_failed_deps(task_id, name, failed_deps)
                else:
                    runnable.append(name)
            if not runnable:
                continue

            agents = [self._agent_registry.get(name) for name in runnable]
            prior_context = "\n\n".join(accumulated_snippets)
            effective_prompt = (
                f"{prompt}\n\n## Results from earlier steps\n{prior_context}" if prior_context else prompt
            )
            failed = await self._execute_agent_group(
                task_id, effective_prompt, agents, workspace, context=context, model_pool=model_pool
            )
            all_failures.extend(failed)

            all_data = await self._task_context_store.get_all(task_id)
            new_snippets = [
                f"[{name}]: {str((all_data.get(name) or {}).get('output', ''))[:_HANDOFF_ARTIFACT_MAX_CHARS]}"
                for name in runnable
                if (all_data.get(name) or {}).get("output")
            ]
            accumulated_snippets.extend(new_snippets)

            # Inject the actual predecessor artifacts (not just the output summary)
            # so downstream agents design/build from the real spec/context, and flag
            # any expected artifact that a successful stage failed to write.
            succeeded = [n for n in runnable if n not in all_failures]
            artifact_snippets, missing = await self._collect_handoff_artifacts(task_id, succeeded)
            accumulated_snippets.extend(artifact_snippets)
            for name in missing:
                await self._warn_missing_handoff_artifact(task_id, name)
        return all_failures

    def _primary_artifact_path(self, agent_name: str, task_id: str) -> Path | None:
        """Resolve a stage's primary declared handoff artifact (its first `produces`)."""
        agent = self._agent_registry.get(agent_name)
        produces = getattr(agent.config, "produces", None) or []
        if not produces:
            return None
        resolved = produces[0].replace("{handoff_dir}", handoff_dir_for(task_id))
        return Path(resolved)

    async def _collect_handoff_artifacts(self, task_id: str, agent_names: list[str]) -> tuple[list[str], list[str]]:
        """Read each stage's primary artifact; return (context snippets, names missing it)."""
        snippets: list[str] = []
        missing: list[str] = []
        for name in agent_names:
            path = self._primary_artifact_path(name, task_id)
            if path is None:
                continue
            content = await asyncio.to_thread(_read_artifact, path, _HANDOFF_ARTIFACT_MAX_CHARS)
            if content:
                snippets.append(f"## {name}'s handoff artifact ({path.name})\n{content}")
            else:
                missing.append(name)
        return snippets, missing

    async def _warn_missing_handoff_artifact(self, task_id: str, agent_name: str) -> None:
        """Surface (ledger + stream) a stage that finished without its expected artifact."""
        path = self._primary_artifact_path(agent_name, task_id)
        artifact = path.name if path is not None else "its handoff artifact"
        logger.warning("handoff: %s completed without writing %s (task %s)", agent_name, artifact, task_id)
        await self._journal.record(
            task_id,
            "handoff_artifact_missing",
            agent=agent_name,
            output=f"{agent_name} finished without writing its expected handoff artifact ({artifact}).",
            payload={"agent": agent_name, "artifact": artifact},
        )

    async def _execute_parallel_groups(
        self,
        task_id: str,
        prompt: str,
        plan: ExecutionPlan,
        workspace: str,
        context: str = "",
        model_pool: str = "reasoning",
    ) -> list[str]:
        """Execute agents in parallel groups."""
        all_failures: list[str] = []
        for group in plan.parallel_groups:
            agents = [self._agent_registry.get(name) for name in group]
            failed = await self._execute_agent_group(
                task_id, prompt, agents, workspace, context=context, model_pool=model_pool
            )
            all_failures.extend(failed)
        return all_failures

    async def _record_task_failure(
        self, task_id: str, task_start: float, output: str, status: str, error_type: str | None = None
    ) -> None:
        """Write a terminal ledger entry for a failed or cancelled task."""
        duration_ms = int((time.monotonic() - task_start) * 1000)
        task_cost_usd = self._tracked_router.pop_task_cost(task_id) if self._tracked_router else 0.0
        action = "task_cancelled" if status == LedgerStatus.CANCELLED else "task_failed"
        await self._journal.write(
            LedgerEntry.new(
                source=LedgerSource.SYSTEM,
                task_id=task_id,
                action=action,
                output=output,
                status=status,
                error_type=error_type,
                duration_ms=duration_ms,
                cost_usd=task_cost_usd,
            )
        )

    async def _record_task_skipped_model_unavailable(self, task_id: str, task_start: float) -> None:
        """Terminal ledger entry for a task skipped because no model was available.

        Stored with LedgerStatus.FAILED so retention, memory consolidation, and
        extraction treat it like any other non-success (nothing to learn from a
        task that never ran) - but with a distinct action that ``get_task`` maps
        to the reported status ``"skipped"``, so the user can tell "ran out of
        model access" apart from "north got it wrong".
        """
        duration_ms = int((time.monotonic() - task_start) * 1000)
        task_cost_usd = self._tracked_router.pop_task_cost(task_id) if self._tracked_router else 0.0
        await self._journal.write(
            LedgerEntry.new(
                source=LedgerSource.SYSTEM,
                task_id=task_id,
                action="task_skipped_model_unavailable",
                output=_MODEL_SCARCITY_MESSAGE,
                status=LedgerStatus.FAILED,
                error_type="model_unavailable",
                duration_ms=duration_ms,
                cost_usd=task_cost_usd,
            )
        )

    async def _report_execution_failures(self, task_id: str, failures: list[str]) -> None:
        """Format and emit a message showing which agents failed to complete."""
        names = ", ".join(f"`{n}`" for n in failures)
        note = f"\n\n> **Note:** {len(failures)} agent(s) did not complete: {names}. Partial results may be missing."
        await self._stream_manager.emit(task_id, "token", {"text": note})

    async def _stage_execute(
        self,
        task_id: str,
        prompt: str,
        plan: ExecutionPlan,
        workspace: str = "",
        domain: str = "general",
        context: str = "",
        confidence: float = 1.0,
    ) -> None:
        """Stage 4: execute the task, then optionally synthesize.

        Engineering work is ONE pipeline with optional phases, chosen by task kind +
        mode (there is no separate "conductor"/"classic" toggle - the conductor IS
        the code path):

          UNDERSTAND  - researcher gathers context (a `question` ends here).
          DESIGN      - architect agrees a spec with the user (`_run_design_phase`);
                        only for feature/refactor when a human is available.
          IMPLEMENT + VERIFY - continuous coder + independent different-model reviewer
                        + Definition-of-Done gate (`_run_engineering_conductor`); for
                        every code kind (bugfix/debug/test/feature/refactor).
          SHIP        - branch/commit/PR, human-gated (`_run_deploy_flow`); `deploy`/`ship`.

        No-code kinds (question/research) and non-engineering domains run the generic
        sequential/parallel executors. Only code kinds get the DoD gate.
        """
        if not workspace:
            workspace = self._default_workspace
        await self._heartbeat(task_id)

        if plan.mode == ExecutionMode.SINGLE_TOOL and plan.direct_tool:
            await self._execute_single_tool(task_id, prompt, plan, workspace, context=context)
            return

        await self._stream_manager.emit(task_id, "executing", {"agents": plan.agents})

        use_conductor = self._use_conductor(domain, plan)
        use_deploy = self._use_deploy_flow(domain, plan)
        use_design = use_conductor and self._use_design_phase(plan)
        model_pool = self._resolve_task_model_pool(plan, domain, confidence)
        if use_deploy:
            all_failures = await self._run_deploy_flow(
                task_id, prompt, workspace, context=context, model_pool=model_pool
            )
        elif use_design:
            # Cockpit: clarify + agree the design with the user first, an independent
            # different-model critique stress-tests the spec, then the continuous coder
            # implements the AGREED spec (resolving the critique within its scope).
            design_failures = await self._run_design_phase(
                task_id, prompt, workspace, context=context, model_pool=model_pool
            )
            if design_failures:
                all_failures = design_failures  # design blocked (incl. no usable spec) - don't implement
            else:
                # Seed the coder's plan from the agreed spec's tasks (resumable state),
                # then stress-test the spec with an independent critique.
                await self._seed_plan_from_spec(task_id)
                spec_critique = await self._rubber_duck_spec(task_id, prompt, workspace)
                all_failures = await self._run_engineering_conductor(
                    task_id,
                    prompt,
                    workspace,
                    self._coder_preamble_for_agreed_spec(task_id, spec_critique),
                    context=context,
                    model_pool=model_pool,
                )
        elif use_conductor:
            all_failures = await self._run_engineering_conductor(
                task_id,
                prompt,
                workspace,
                self._coder_preamble_for_kind(plan.engineering_kind),
                context=context,
                model_pool=model_pool,
            )
        elif plan.mode == ExecutionMode.HIERARCHICAL:
            all_failures = await self._execute_hierarchical_groups(
                task_id, prompt, plan, workspace, context=context, model_pool=model_pool
            )
        else:
            all_failures = await self._execute_parallel_groups(
                task_id, prompt, plan, workspace, context=context, model_pool=model_pool
            )

        if all_failures:
            await self._report_execution_failures(task_id, all_failures)

        # Definition-of-Done gate: ENFORCED only on the conductor (code-change) path -
        # a task whose recorded evidence doesn't clear the bar (a code change was
        # applied + an independent, different-model review passed) finishes
        # completed-with-failures with the reasons surfaced, so it is never reported
        # as a clean success it didn't earn. Non-code engineering kinds (question/
        # research) run the classic path and are not gated by a code DoD. Fails open.
        dod_unmet_reasons: list[str] | None = None
        if use_conductor:
            # Independent executable oracle: the orchestrator runs the project's
            # verification command itself (not the model's word) and feeds the result
            # into the DoD. Only runs when a code change was applied (else there is
            # nothing to verify), and only for real coding tasks (this branch).
            auto_verify = await self._quality_gate.run_verification(task_id, workspace) if not all_failures else None
            dod = await self._quality_gate.evaluate(task_id, domain, plan.engineering_kind, auto_verify)
            if dod is not None and not dod.passed:
                dod_unmet_reasons = dod.reasons
                await self._quality_gate.report_unmet(task_id, dod.reasons)
            # The conductor's own coder/reviewer output is streamed live; a synthesis
            # over plan.agents (which may name researcher/architect that never ran)
            # would summarise empty outputs, so it is skipped here.
        elif use_deploy:
            # Deploy streams the agent's shipping report live; there is no new code to
            # verify (no DoD) and a single-agent synthesis would just be redundant.
            pass
        else:
            await self._maybe_synthesize(task_id, plan.agents, plan.mode, failures=all_failures)

        # Episodes are written by the background EpisodeConsolidator (single writer)
        # from the ledger, covering success, failure, and cancellation uniformly.
        if use_deploy:
            total_agents = 1
        elif use_design:
            total_agents = 4  # researcher + architect (design) + coder + reviewer
        elif use_conductor:
            total_agents = 2  # coder + reviewer
        else:
            total_agents = len(plan.agents)
        await self._finish_task(
            task_id, failures=all_failures, total_agents=total_agents, dod_unmet_reasons=dod_unmet_reasons
        )

    async def _execute_single_tool(
        self, task_id: str, prompt: str, plan: ExecutionPlan, workspace: str, context: str = ""
    ) -> None:
        """Execute a single tool call directly, bypassing the agent layer."""
        await self._stream_manager.emit(task_id, "executing", {"agents": []})
        await self._stream_manager.emit(
            task_id, "tool_called", {"tool": plan.direct_tool, "params": plan.direct_tool_params}
        )

        output = ""
        success = False
        try:
            if self._tool_registry is None:
                raise ToolNotFoundError("No tool registry available.")
            tool = self._tool_registry.get(plan.direct_tool)  # type: ignore[arg-type]
            params = {**plan.direct_tool_params}
            if workspace and "workspace" not in params:
                params["workspace"] = workspace
            if task_id and "task_id" not in params:
                params["task_id"] = task_id
            result = await tool.run(ToolInput(params=params))
            success = result.success
            output = tool.format_output(result.data) if result.success else f"Tool error: {result.error}"
            # No image-interpretation branch here on purpose: every tool that returns
            # an image (take_screenshot, take_photo) is in the planner's
            # _INTERPRETATION_TOOLS set, so it can never be routed to single-tool
            # mode - it always runs inside an agent's ReAct loop, which feeds the
            # image to the model as a proper image message. A second synthesis path
            # here would be unreachable and worse.
            if success and getattr(tool, "is_mutating", False) and self._running_task_store is not None:
                await self._running_task_store.mark_side_effect(task_id)
        except ToolNotFoundError:
            logger.warning("single_tool fallback: tool %r not found, re-routing to agent", plan.direct_tool)
            fallback = self._execution_planner.build_fallback_plan("general", task_id)
            await self._stream_manager.emit(task_id, "executing", {"agents": fallback.agents})
            fallback_failures = await self._execute_parallel_groups(
                task_id, prompt, fallback, workspace, context=context
            )
            if fallback_failures:
                await self._report_execution_failures(task_id, fallback_failures)
            await self._finish_task(task_id, failures=fallback_failures, total_agents=len(fallback.agents))
            return
        except Exception as exc:
            logger.warning("Direct tool execution error in task %s: %s", task_id, exc, exc_info=True)
            output = f"Tool execution error: {exc}"
            success = False

        await self._stream_manager.emit(task_id, "tool_result", {"tool": plan.direct_tool, "success": success})

        await self._journal.record(
            task_id,
            "agent_completed",
            source=LedgerSource.AGENT,
            agent="tool_executor",
            output=output,
            event="token",
            payload={"text": output},
        )
        await self._finish_task(task_id, skip_extraction=True)

    async def _finish_task(
        self,
        task_id: str,
        *,
        skip_extraction: bool = False,
        failures: list[str] | None = None,
        total_agents: int = 0,
        dod_unmet_reasons: list[str] | None = None,
    ) -> None:
        """Write the terminal ledger entry and emit done events.

        When every agent failed the task finishes as FAILED; partial failures - or an
        unmet Definition of Done (dod_unmet_reasons) - finish as COMPLETED but with a
        distinct action so the history shows the task did not fully succeed.
        """
        failures = failures or []
        scarcity = _is_model_scarcity(failures)
        all_failed = total_agents > 0 and len(failures) >= total_agents
        dod_failed = bool(dod_unmet_reasons)
        task_cost_usd = self._tracked_router.pop_task_cost(task_id) if self._tracked_router else 0.0
        if scarcity:
            # No model was available to do the work - a graceful, honest skip, not
            # a north failure. Checked first so a scarcity blockage is never
            # reported as task_failed or masked by a downstream DoD symptom.
            action, status, err = "task_skipped_model_unavailable", LedgerStatus.FAILED, "model_unavailable"
        elif all_failed:
            action, status, err = "task_failed", LedgerStatus.FAILED, "agent_failure"
        elif failures or dod_failed:
            action, status, err = (
                "task_completed_with_failures",
                LedgerStatus.COMPLETED,
                "dod_unmet" if dod_failed else None,
            )
        else:
            action, status, err = "task_completed", LedgerStatus.COMPLETED, None
        output_parts: list[str] = []
        if scarcity:
            output_parts.append(f"Skipped: {_MODEL_SCARCITY_MESSAGE}")
        if failures:
            output_parts.append(f"Failed agents: {', '.join(failures)}")
        if dod_failed:
            output_parts.append(f"Definition of Done not met: {'; '.join(dod_unmet_reasons)}")
        await self._journal.write(
            LedgerEntry.new(
                source=LedgerSource.SYSTEM,
                task_id=task_id,
                action=action,
                output=" | ".join(output_parts) or None,
                status=status,
                error_type=err,
                cost_usd=task_cost_usd,
            )
        )
        if scarcity:
            await self._stream_manager.emit(
                task_id,
                "task_skipped",
                {
                    "reason": _MODEL_SCARCITY_MESSAGE,
                    "skipped_agents": [str(f) for f in failures],
                    "cost_usd": task_cost_usd,
                },
            )
        elif all_failed:
            await self._stream_manager.emit(
                task_id,
                "task_failed",
                {
                    "error": f"All agents failed: {', '.join(failures)}",
                    "error_type": "agent_failure",
                    "cost_usd": task_cost_usd,
                },
            )
        else:
            await self._stream_manager.emit(
                task_id,
                "task_completed",
                {
                    "cost_usd": task_cost_usd,
                    "failed_agents": [str(f) for f in failures],
                    "dod_unmet": dod_unmet_reasons or [],
                },
            )
        await self._stream_manager.emit_done(task_id)
        # Trigger extraction immediately after agent tasks so preferences stated
        # mid-task land in judgement_rules.md before the next task starts.
        # Single-tool tasks (deterministic, no agent reasoning) are skipped  -
        # they produce no signal worth extracting.
        if self._extraction_pipeline is not None and not skip_extraction:
            spawn(self._extraction_pipeline.run_once(), name="extraction_run_once")

    async def _maybe_synthesize(
        self,
        task_id: str,
        agents: list[str],
        mode: ExecutionMode = ExecutionMode.PARALLEL,
        failures: list[str] | None = None,
    ) -> None:
        """Synthesize outputs from multiple agents into one response, if applicable."""
        if failures:
            return  # Partial data - skip synthesis to avoid a confidently wrong summary.
        if self._synthesizer is None or len(agents) < 2:
            return
        if mode not in (ExecutionMode.PARALLEL, ExecutionMode.HIERARCHICAL):
            return

        all_data = await self._task_context_store.get_all(task_id)
        agent_outputs = {agent: (all_data.get(agent) or {}).get("output", "") for agent in agents}

        synthesized = await self._synthesizer.synthesize(agent_outputs, task_id)
        if synthesized is None:
            return

        await self._stream_manager.emit(
            task_id,
            "task_synthesis",
            {"output": synthesized, "agents": agents},
        )

    async def _execute_agent_group(
        self,
        task_id: str,
        prompt: str,
        agents: list[Agent],
        workspace: str = "",
        context: str = "",
        allow_delegation: bool = True,
        model_pool: str = "reasoning",
    ) -> list[str]:
        """Run a parallel group of agents concurrently; handle per-agent failures.

        Returns the names of any agents that failed after all retries. Pass
        ``allow_delegation=False`` to run the agents in report-only mode (the
        conductor uses this for the reviewer so it never delegates a fix back to the
        coder - the orchestrator owns that fix loop).
        """
        await self._heartbeat(task_id)
        # Per-agent payloads: an agent declaring `distinct_from` in its config (e.g.
        # the reviewer, distinct_from: coder) is run with the other agent's model in
        # exclude_models, so it is a genuine independent second opinion.
        payloads = [
            AgentPayload(
                task_id=task_id,
                prompt=prompt,
                workspace=workspace,
                context=context,
                model_pool=model_pool,
                exclude_models=await self._exclude_models_for(task_id, agent),
                allow_delegation=allow_delegation,
            )
            for agent in agents
        ]
        results = await asyncio.gather(
            *[self._isolation.run(agent, p) for agent, p in zip(agents, payloads, strict=False)],
            return_exceptions=True,
        )

        failed: list[str] = []
        for agent, payload, result in zip(agents, payloads, results, strict=False):
            if isinstance(result, asyncio.CancelledError):
                # A cancelled task means cancel_task() was called - propagate
                # so the outer _process_task handler can write the ledger entry.
                raise result
            if isinstance(result, Exception):
                error_type = classify_error(result)
                logger.error(
                    "Agent '%s' failed in task '%s' - error_type=%s: %s",
                    agent.name,
                    task_id,
                    error_type,
                    result,
                    exc_info=result,
                )
                failed.append(AgentFailure(agent.name, error_type))
                await self._journal.write(
                    LedgerEntry.new(
                        source=LedgerSource.AGENT,
                        task_id=task_id,
                        agent=agent.name,
                        action="agent_execution_failed",
                        output=str(result),
                        status=LedgerStatus.FAILED,
                        error_type=error_type,
                    )
                )
            else:
                await self._handle_agent_result(task_id, agent, result, payload)
        return failed

    async def _models_used_by(self, task_id: str, agent_names: set[str]) -> list[str]:
        """Models the named agents used in this task, from their agent_completed entries.

        De-duplicated in first-seen order; [] on any error. Used both to force an
        independent second opinion (exclude a prior agent's model) and to check that a
        critique actually ran on a different model.
        """
        if not agent_names:
            return []
        try:
            entries = await self._ledger.query_summaries(LedgerFilters(task_id=task_id, limit=200))
        except Exception:
            logger.debug("model lookup failed for task %s", task_id, exc_info=True)
            return []
        models: list[str] = []
        seen: set[str] = set()
        for entry in entries:
            if entry.action != "agent_completed" or entry.agent not in agent_names:
                continue
            for model in (entry.model_used or "").split(","):
                model = model.strip()
                if model and model not in seen:
                    seen.add(model)
                    models.append(model)
        return models

    async def _exclude_models_for(self, task_id: str, agent: Agent) -> list[str]:
        """Models *agent* must avoid this run, from its config's `distinct_from`.

        Looks up the model(s) the named agents already used for this task so, e.g.,
        the reviewer is forced onto a different model than the coder. Returns [] when
        there is nothing to exclude.
        """
        distinct_from = getattr(agent.config, "distinct_from", None) or []
        if not isinstance(distinct_from, list) or not distinct_from:
            return []
        return await self._models_used_by(task_id, set(distinct_from))

    def _maybe_refresh_pools_background(self) -> None:
        if self._tracked_router is None:
            return
        now = time.monotonic()
        if now - self._last_pool_refresh_at < POOL_REFRESH_COOLDOWN:
            return
        self._last_pool_refresh_at = now

        async def _refresh() -> None:
            try:
                await self._tracked_router.refresh_pools()  # type: ignore[union-attr]
                logger.info("Inference pool refreshed after agent failure")
            except Exception:
                logger.warning("Post-failure inference pool refresh failed", exc_info=True)

        spawn(_refresh(), name="pool_refresh_after_failure")

    async def _run_agent_with_retry(self, agent: Agent, payload: AgentPayload) -> AgentResult:
        """Run an agent, retrying on failure up to the handler's max_retries."""
        task_id = payload.task_id
        while True:
            await self._stream_manager.emit(
                task_id,
                "agent_started",
                {
                    "agent": agent.name,
                    "task": payload.prompt[:100],
                    "run_id": payload.run_id,
                    "parent_run_id": payload.parent_run_id,
                    "attempt": payload.attempt,
                },
            )
            t0 = time.monotonic()
            try:
                result = await agent.run(payload)
                result.duration_ms = int((time.monotonic() - t0) * 1000)
                self._failure_handler.clear_retry_count(task_id, agent.name)
                await self._task_context_store.update_agent_status(task_id, agent.name, "completed")
                await self._stream_manager.emit(
                    task_id,
                    "agent_completed",
                    {
                        "agent": agent.name,
                        "summary": result.summary,
                        "duration_ms": result.duration_ms,
                        "run_id": result.run_id,
                        "parent_run_id": result.parent_run_id,
                        "attempt": result.attempt,
                    },
                )
                return result
            except asyncio.CancelledError:
                self._failure_handler.clear_retry_count(task_id, agent.name)
                raise
            except Exception as exc:
                should_retry = await self._failure_handler.handle_failure(task_id, agent.name, exc)
                if not should_retry:
                    await self._stream_manager.emit(
                        task_id,
                        "agent_failed",
                        {
                            "agent": agent.name,
                            "error": str(exc)[:500],
                            "run_id": payload.run_id,
                            "parent_run_id": payload.parent_run_id,
                            "attempt": payload.attempt,
                        },
                    )
                    raise
                # The failed attempt may have streamed partial output - tell
                # the UI to discard it before the retry re-streams the answer.
                await self._stream_manager.emit(
                    task_id,
                    "stream_reset",
                    {
                        "agent": agent.name,
                        "run_id": payload.run_id,
                        "parent_run_id": payload.parent_run_id,
                        "attempt": payload.attempt,
                    },
                )
                payload = payload.model_copy(update={"run_id": generate_id(), "attempt": payload.attempt + 1})
                self._maybe_refresh_pools_background()

    async def _handle_agent_result(
        self, task_id: str, agent: Agent, result: AgentResult, payload: AgentPayload | None = None
    ) -> None:
        """Write result to task context, ledger, and notify user if needed."""
        await self._auditor.audit(task_id, agent, result, payload)
        # Verification/self-repair can refine the result after Agent.run() first
        # persisted it. Keep the run index aligned with the final audited output.
        if result.run_id and agent.deps.agent_run_store is not None:
            await agent.deps.agent_run_store.complete(result.run_id, result)
        await self._task_context_store.write(
            task_id=task_id,
            agent=agent.name,
            key="result",
            value=result.data,
            status="completed",
        )
        await self._task_context_store.write(
            task_id=task_id,
            agent=agent.name,
            key="output",
            value=result.output,
            status="completed",
        )

        await self._journal.write(
            LedgerEntry.new(
                source=LedgerSource.AGENT,
                task_id=task_id,
                run_id=result.run_id,
                parent_run_id=result.parent_run_id,
                attempt=result.attempt,
                agent=agent.name,
                action="agent_completed",
                output=result.output,
                agent_output=result.data,
                tools_used=result.tools_used,
                status=LedgerStatus.COMPLETED,
                duration_ms=result.duration_ms,
                cost_usd=result.cost_usd,
                tokens_in=result.tokens_in,
                tokens_out=result.tokens_out,
                model_used=", ".join(result.models_used) or None,
            )
        )

        if result.requires_approval or result.has_question:
            card_type = CardType.QUESTION if result.has_question else CardType.APPROVAL
            card = Card(
                id=generate_id(),
                type=card_type,
                task_id=task_id,
                agent=agent.name,
                title=f"{agent.name.capitalize()} Agent - Action Required",
                message=result.question or result.output,
                options=result.question_options if result.has_question else ["Approve", "Reject"],
            )
            await self._interaction.notify(card)
        else:
            card = Card(
                id=generate_id(),
                type=CardType.INFORMATION,
                task_id=task_id,
                agent=agent.name,
                title=f"{agent.name.capitalize()} - Done",
                message=result.summary,
            )
            await self._interaction.notify(card)
