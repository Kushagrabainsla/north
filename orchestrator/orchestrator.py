"""Main Orchestrator - ties Stages 1–4.

See docs/CODING_STYLE.md Sections 2.5, 4.1, 6, 10.2, 14.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from agents import Agent, AgentPayload, AgentResult
from agents.constants import ENGINEERING_AGENTS
from agents.registry import AgentRegistry
from agents.workspace_lock import workspace_lock
from approval import ApprovalDecision, Card, CardType, JudgementFilter, Notifier, UserInteraction
from approval.approval_memory import ApprovalMemory
from approval.store import ApprovalStore
from config.strategy import NorthSettings, StrategyMode, describe
from inference.cost_tracker import CostTracker
from inference.models import CompletionRequest, PoolPriority
from ledger import LedgerEntry, LedgerFilters, LedgerSource, LedgerStatus, LedgerWriter
from orchestrator.best_of_n import CandidateOutcome, any_viable, select_best
from orchestrator.constants import (
    MAX_CONCURRENT_TASKS,
    NORTH_STAR_CONFIDENCE_THRESHOLD,
    POOL_REFRESH_COOLDOWN,
    STRATEGY_CMD_RE,
    WORKTREE_ISOLATION_AGENTS,
)
from orchestrator.exceptions import NorthStarConflictError, OrchestratorError, TaskCapacityError
from orchestrator.failure_handler import FailureHandler, classify_error
from orchestrator.idempotency import IdempotencyCache, idempotency_key
from orchestrator.models import (
    ExecutionMode,
    ExecutionPlan,
    IntentClassification,
    TaskRequest,
    TaskResponse,
)
from orchestrator.north_star import NorthStarChecker
from orchestrator.router import ExecutionPlanner
from orchestrator.running_tasks import RunningTaskStore
from orchestrator.stream import EventStreamManager
from orchestrator.synthesizer import ResultSynthesizer
from orchestrator.task_context import TaskContextStore
from orchestrator.verification import verify_claims
from orchestrator.worktree import GitWorktreeManager, IntegrationResult, Worktree, WorktreeError
from tools._path import handoff_dir_for
from tools.exceptions import ToolNotFoundError
from tools.models import ToolInput
from tools.registry import ToolRegistry
from utils.ids import generate_id, generate_task_id
from utils.logging import bind_task_id
from utils.tasks import spawn
from utils.time import format_timestamp, utcnow

logger = logging.getLogger(__name__)

# Max characters of a handoff artifact injected into a downstream agent's context.
_HANDOFF_ARTIFACT_MAX_CHARS: int = 6000

# Engineering evidence gate (#1): a code change with no run of one of the verify
# tools means the agent never checked its own work.
_CODE_MUTATING_TOOLS: frozenset[str] = frozenset({"write_file", "patch_file"})
_CODE_VERIFY_TOOLS: frozenset[str] = frozenset({"check_types", "bash", "lint"})

# Coder<->reviewer loop (#5): extra coder→reviewer rounds allowed when review
# reports failing tests, and the conservative signal that the reviewer found failures.
_QA_MAX_EXTRA_ROUNDS: int = 2
# Per-candidate test-command timeout for best-of-N selection (#11).
_BEST_OF_N_TEST_TIMEOUT: int = 300
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
}
_REVIEW_FAILURE_RE = re.compile(
    r"\b(?:[1-9]\d*\s+failed|tests?\s+(?:are\s+)?fail(?:ed|ing)|test\s+suite\s+failed"
    r"|failing\s+tests?|did\s+not\s+pass|not\s+passing|assertion\s*error)\b",
    re.IGNORECASE,
)


def _read_artifact(path: Path, max_chars: int) -> str | None:
    """Read a handoff artifact file, capped; None if missing, unreadable, or empty."""
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

# Ledger status recorded for each approval-card decision. Answers to questions are
# handled separately (recorded as learnable clarifications), so they are absent here.
_APPROVAL_DECISION_STATUS: dict[str, LedgerStatus] = {
    ApprovalDecision.APPROVED: LedgerStatus.APPROVED,
    ApprovalDecision.REJECTED: LedgerStatus.REJECTED,
    ApprovalDecision.TIMEOUT_REJECTED: LedgerStatus.REJECTED,
}

_CRITIC_PROMPT = """\
You are a strict reviewer for a personal assistant called north. Judge only whether
the assistant's answer actually addresses the user's request. Do not rewrite it.

User request:
---
{request}
---

Assistant answer:
---
{answer}
---

Reply with JSON only:
{{"adequate": true or false, "gap": "<one short sentence naming what is missing, or empty>"}}

Rules:
- "adequate" is true when the answer meaningfully addresses the request, even if brief.
- Set "adequate" false only for a real, specific gap: an unanswered part, the wrong
  target, or an empty/placeholder answer.
- When unsure, return "adequate": true - false positives annoy the user.
"""


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
        running_task_store: RunningTaskStore | None = None,
        stuck_task_max_age_seconds: int = 86_400,
        self_repair: bool = True,
        idempotency_window_seconds: int = 60,
        critic: bool = False,
        approval_memory: ApprovalMemory | None = None,
    ) -> None:
        self._ledger = ledger
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
        self._default_workspace = default_workspace
        self._extraction_pipeline = extraction_pipeline
        self._worktree_isolation = worktree_isolation
        self._worktree_root = worktree_root
        self._best_of_n = max(1, best_of_n)
        self._best_of_n_test_command = best_of_n_test_command.strip()
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
                    if existing is not None:
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
            await self._write_ledger(
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

            await self._write_ledger(
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

    async def get_task(self, task_id: str) -> TaskResponse | None:
        """Return the current status of a task.

        A task's status is its most recent *terminal* ledger entry (completed /
        failed / cancelled). Intermediate steps - classification and each agent -
        are logged COMPLETED per step, so reading the single most recent entry
        would report a still-running task as "completed" the instant it was
        classified. We scan for the terminal action instead, and report "pending"
        while the task is still in flight.
        """
        entries = await self._ledger.query(LedgerFilters(task_id=task_id, limit=100))
        if not entries:
            return None
        for entry in entries:  # query() returns most-recent-first
            terminal = _TERMINAL_TASK_ACTIONS.get(entry.action)
            if terminal is not None:
                return TaskResponse(
                    task_id=task_id, status=terminal, created_at=format_timestamp(entry.timestamp)
                )
        # No terminal entry yet - the task is still running.
        return TaskResponse(
            task_id=task_id, status="pending", created_at=format_timestamp(entries[-1].timestamp)
        )

    async def cancel_task(self, task_id: str) -> bool:
        """Cancel a running task: stop its pipeline and write a terminal ledger entry.

        Returns False when the task is not in flight (unknown id or already
        finished) - writing a CANCELLED entry then would rewrite the history
        of a completed task, since get_task() reads the most recent entry.
        """
        running = self._active_tasks.pop(task_id, None)
        if running is None:
            return False
        if not running.done():
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
        await self._write_ledger(
            LedgerEntry.new(
                source=LedgerSource.SYSTEM,
                task_id=task_id,
                action="task_cancelled",
                status=LedgerStatus.CANCELLED,
            )
        )
        await self._stream_manager.emit(task_id, "task_cancelled", {})
        await self._stream_manager.emit_done(task_id)
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
        await self._write_ledger(
            LedgerEntry.new(
                source=source,
                task_id=card.task_id,
                agent=card.agent,
                action=action,
                input=ledger_input,
                output=f"chosen_option={chosen_option or decision}",
                status=status,
            )
        )
        await self._stream_manager.emit(
            card.task_id,
            "approval_responded",
            {
                "card_id": card_id,
                "decision": decision,
                "chosen_option": chosen_option,
            },
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
        """Fail a task the watchdog found stalled: record why, then cancel it.

        Cancelling routes through cancel_task(), whose _process_task unwind clears
        the running-task registry, so no extra cleanup is needed here.
        """
        await self._write_ledger(
            LedgerEntry.new(
                source=LedgerSource.SYSTEM,
                task_id=task_id,
                action="task_stuck",
                output=(
                    f"No progress for over {self._stuck_task_max_age_seconds}s - "
                    "cancelling as stuck (watchdog)."
                ),
                status=LedgerStatus.FAILED,
                error_type="stuck_timeout",
            )
        )
        cancelled = await self.cancel_task(task_id)
        if not cancelled and self._running_task_store is not None:
            # Not in flight (e.g. a stale row with no live task): drop it directly.
            await self._running_task_store.clear(task_id)
        return cancelled

    async def list_active_tasks(self) -> list[TaskResponse]:
        """Returns tasks that are currently in-flight (asyncio tasks still running)."""
        responses = await asyncio.gather(*(self.get_task(task_id) for task_id in list(self._active_tasks)))
        return [resp for resp in responses if resp is not None]

    # ------------------------------------------------------------------ #
    #  Internal helpers                                                    #
    # ------------------------------------------------------------------ #

    async def _write_ledger(self, entry: LedgerEntry) -> None:
        """Ledger write with error logging; safe to fire-and-forget."""
        try:
            await self._ledger.write(entry)
        except Exception as exc:
            logger.error(
                "Ledger write failed task=%s action=%s: %s",
                entry.task_id,
                entry.action,
                exc,
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
        await self._write_ledger(
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

        self._north_settings.set_strategy(mode)
        msg = f"Strategy set to **{mode.value}**. {describe(mode)}"
        await self._write_ledger(
            LedgerEntry.new(
                source=LedgerSource.SYSTEM,
                task_id=task_id,
                action="agent_completed",
                agent="orchestrator",
                output=msg,
                status=LedgerStatus.COMPLETED,
            )
        )
        await self._stream_manager.emit(task_id, "task_completed", {})
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

            classification, plan = await self._stage_plan(task_id, request.prompt, request.context)
            await self._stage_north_star(task_id, request.prompt, classification)
            await self._stage_execute(
                task_id,
                request.prompt,
                plan,
                request.workspace,
                domain=classification.domain,
                context=request.context,
            )
        except asyncio.CancelledError:
            # cancel_task() already wrote the ledger entry and emitted events.
            raise
        except NorthStarConflictError as e:
            logger.warning("Task %s rejected: conflicts with North Star goals", task_id)
            await self._task_context_store.update_task_status(task_id, "failed")
            await self._stream_manager.emit(task_id, "task_rejected", {"reason": str(e)})
            await self._record_task_failure(task_id, task_start, str(e), LedgerStatus.CANCELLED, "north_star_conflict")
            await self._stream_manager.emit_done(task_id)
        except Exception as e:
            logger.error("Task %s failed: %s", task_id, e, exc_info=True)
            await self._task_context_store.update_task_status(task_id, "failed")
            error_type = classify_error(e)
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
            # The task has reached a terminal state (success/failure/cancel), so it
            # is no longer in-flight: drop it from the crash-recovery registry.
            if self._running_task_store is not None:
                await self._running_task_store.clear(task_id)

    async def _stage_plan(
        self, task_id: str, prompt: str, context: str = ""
    ) -> tuple[IntentClassification, ExecutionPlan]:
        """Stages 1+3: Classify intent and build execution plan in one LLM call."""
        await self._stream_manager.emit(task_id, "classifying", {"prompt": prompt})

        classification, plan = await self._execution_planner.plan_all(prompt, task_id=task_id, context=context)

        await self._write_ledger(
            LedgerEntry.new(
                source=LedgerSource.SYSTEM,
                task_id=task_id,
                action=f"classified_as_{'consequential' if classification.is_consequential else 'trivial'}",
                output=classification.reasoning,
                status=LedgerStatus.COMPLETED,
                # Stamp the domain so the episode consolidator can tag each task's
                # episode without re-deriving it (used for per-agent gating).
                agent_output={"domain": classification.domain},
            )
        )

        await self._stream_manager.emit(
            task_id,
            "classified",
            {
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
            # Fail CLOSED: a consequential task whose alignment cannot be
            # evaluated is blocked, not waved through. The user can resubmit
            # once inference is available again.
            logger.warning("North Star check failed - blocking task (fail closed): %s", e)
            await self._write_ledger(
                LedgerEntry.new(
                    source=LedgerSource.SYSTEM,
                    task_id=task_id,
                    action="north_star_check_failed",
                    output=str(e),
                    status=LedgerStatus.FAILED,
                )
            )
            await self._stream_manager.emit(task_id, "north_star_check_failed", {"reason": str(e)})
            raise NorthStarConflictError(
                f"North Star alignment could not be evaluated - task blocked (fail closed): {e}"
            ) from e

        check_action = "north_star_check_aligned" if aligned else "north_star_check_conflict"
        await self._write_ledger(
            LedgerEntry.new(
                source=LedgerSource.SYSTEM,
                task_id=task_id,
                action=check_action,
                output=reasoning,
                status=LedgerStatus.COMPLETED,
            )
        )

        if not aligned:
            await self._handle_alignment_conflict(task_id, tension)

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
        await self._write_ledger(
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

    async def _execute_hierarchical_groups(
        self, task_id: str, prompt: str, plan: ExecutionPlan, workspace: str, context: str = ""
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
            failed = await self._execute_agent_group(task_id, effective_prompt, agents, workspace, context=context)
            all_failures.extend(failed)

            all_data = await self._task_context_store.get_all(task_id)
            new_snippets = [
                f"[{name}]: {(all_data.get(name) or {}).get('output', '')}"
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
        if "reviewer" not in all_failures:
            await self._run_qa_loop_if_failing(task_id, prompt, plan, workspace, context)
        return all_failures

    async def _run_qa_loop_if_failing(
        self, task_id: str, prompt: str, plan: ExecutionPlan, workspace: str, context: str
    ) -> None:
        """Re-run coder→reviewer while review reports failing tests, capped (#5).

        A conservative safety net over the agent-driven delegation loop: only the
        coder and reviewer are re-run, only when the reviewer's report clearly signals
        failures, and only for a bounded number of extra rounds. Stops as soon as
        the failure signal clears or a re-run itself fails.
        """
        if "coder" not in plan.agents or "reviewer" not in plan.agents:
            return
        coder = self._agent_registry.get("coder")
        reviewer = self._agent_registry.get("reviewer")
        for round_num in range(1, _QA_MAX_EXTRA_ROUNDS + 1):
            all_data = await self._task_context_store.get_all(task_id)
            qa_report = (all_data.get("reviewer") or {}).get("output", "") or ""
            if not _REVIEW_FAILURE_RE.search(qa_report):
                return  # review clean (or no failure reported) - nothing to loop on
            await self._stream_manager.emit(task_id, "qa_loop", {"round": round_num})
            fix_prompt = (
                f"{prompt}\n\n## Review reported issues - fix them, then verify\n"
                f"{qa_report[:_HANDOFF_ARTIFACT_MAX_CHARS]}"
            )
            if await self._execute_agent_group(task_id, fix_prompt, [coder], workspace, context=context):
                return
            retest = f"{prompt}\n\n## Re-run the tests and re-review after the coder's fix and report results."
            if await self._execute_agent_group(task_id, retest, [reviewer], workspace, context=context):
                return

    def _primary_artifact_path(self, agent_name: str, task_id: str) -> Path | None:
        """Resolve a stage's primary declared handoff artifact (its first `produces`)."""
        agent = self._agent_registry.get(agent_name)
        produces = getattr(agent.config, "produces", None) or []
        if not produces:
            return None
        resolved = produces[0].replace("{handoff_dir}", handoff_dir_for(task_id))
        return Path(resolved)

    async def _collect_handoff_artifacts(
        self, task_id: str, agent_names: list[str]
    ) -> tuple[list[str], list[str]]:
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
        await self._write_ledger(
            LedgerEntry.new(
                source=LedgerSource.SYSTEM,
                task_id=task_id,
                agent=agent_name,
                action="handoff_artifact_missing",
                output=f"{agent_name} finished without writing its expected handoff artifact ({artifact}).",
                status=LedgerStatus.COMPLETED,
            )
        )
        await self._stream_manager.emit(
            task_id, "handoff_artifact_missing", {"agent": agent_name, "artifact": artifact}
        )

    async def _execute_parallel_groups(
        self, task_id: str, prompt: str, plan: ExecutionPlan, workspace: str, context: str = ""
    ) -> list[str]:
        """Execute agents in parallel groups."""
        all_failures: list[str] = []
        for group in plan.parallel_groups:
            agents = [self._agent_registry.get(name) for name in group]
            failed = await self._execute_agent_group(task_id, prompt, agents, workspace, context=context)
            all_failures.extend(failed)
        return all_failures

    async def _record_task_failure(
        self, task_id: str, task_start: float, output: str, status: str, error_type: str | None = None
    ) -> None:
        """Write a terminal ledger entry for a failed or cancelled task."""
        duration_ms = int((time.monotonic() - task_start) * 1000)
        task_cost_usd = self._tracked_router.pop_task_cost(task_id) if self._tracked_router else 0.0
        action = "task_cancelled" if status == LedgerStatus.CANCELLED else "task_failed"
        await self._write_ledger(
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
    ) -> None:
        """Stage 4: Execute agents in dependency order, then optionally synthesize."""
        if not workspace:
            workspace = self._default_workspace
        await self._heartbeat(task_id)

        if plan.mode == ExecutionMode.SINGLE_TOOL and plan.direct_tool:
            await self._execute_single_tool(task_id, prompt, plan, workspace, context=context)
            return

        await self._stream_manager.emit(task_id, "executing", {"agents": plan.agents})

        if plan.mode == ExecutionMode.HIERARCHICAL:
            all_failures = await self._execute_hierarchical_groups(task_id, prompt, plan, workspace, context=context)
        else:
            all_failures = await self._execute_parallel_groups(task_id, prompt, plan, workspace, context=context)

        if all_failures:
            await self._report_execution_failures(task_id, all_failures)

        await self._maybe_synthesize(task_id, plan.agents, plan.mode, failures=all_failures)

        # Episodes are written by the background EpisodeConsolidator (single writer)
        # from the ledger, covering success, failure, and cancellation uniformly.
        await self._finish_task(task_id, failures=all_failures, total_agents=len(plan.agents))

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

        await self._write_ledger(
            LedgerEntry.new(
                source=LedgerSource.AGENT,
                task_id=task_id,
                agent="tool_executor",
                action="agent_completed",
                output=output,
                status=LedgerStatus.COMPLETED,
            )
        )

        await self._stream_manager.emit(task_id, "token", {"text": output})
        await self._finish_task(task_id, skip_extraction=True)

    async def _finish_task(
        self,
        task_id: str,
        *,
        skip_extraction: bool = False,
        failures: list[str] | None = None,
        total_agents: int = 0,
    ) -> None:
        """Write the terminal ledger entry and emit done events.

        When every agent failed the task finishes as FAILED; partial failures
        finish as COMPLETED but with a distinct action so the history shows
        the task did not fully succeed.
        """
        failures = failures or []
        all_failed = total_agents > 0 and len(failures) >= total_agents
        task_cost_usd = self._tracked_router.pop_task_cost(task_id) if self._tracked_router else 0.0
        if all_failed:
            action, status = "task_failed", LedgerStatus.FAILED
        elif failures:
            action, status = "task_completed_with_failures", LedgerStatus.COMPLETED
        else:
            action, status = "task_completed", LedgerStatus.COMPLETED
        await self._write_ledger(
            LedgerEntry.new(
                source=LedgerSource.SYSTEM,
                task_id=task_id,
                action=action,
                output=f"Failed agents: {', '.join(failures)}" if failures else None,
                status=status,
                error_type="agent_failure" if all_failed else None,
                cost_usd=task_cost_usd,
            )
        )
        if all_failed:
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
                {"cost_usd": task_cost_usd, "failed_agents": failures},
            )
        await self._stream_manager.emit_done(task_id)
        # Release the in-memory Condition for this task; DB rows are kept for
        # the retention window but no more readers will wait on this task_id.
        self._task_context_store.release_conditions(task_id)
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
        self, task_id: str, prompt: str, agents: list[Agent], workspace: str = "", context: str = ""
    ) -> list[str]:
        """Run a parallel group of agents concurrently; handle per-agent failures.

        Returns the names of any agents that failed after all retries.
        """
        payload = AgentPayload(task_id=task_id, prompt=prompt, workspace=workspace, context=context)
        await self._heartbeat(task_id)
        results = await asyncio.gather(
            *[self._run_agent_isolated_or_direct(agent, payload) for agent in agents],
            return_exceptions=True,
        )

        failed: list[str] = []
        for agent, result in zip(agents, results, strict=False):
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
                failed.append(agent.name)
                await self._write_ledger(
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

    def _should_isolate(self, agent: Agent, payload: AgentPayload) -> bool:
        """True when this agent's run should happen in an isolated git worktree."""
        return self._worktree_isolation and bool(payload.workspace) and agent.name in WORKTREE_ISOLATION_AGENTS

    async def _run_agent_isolated_or_direct(self, agent: Agent, payload: AgentPayload) -> AgentResult:
        """Run *agent* in a dedicated git worktree when isolation applies, else directly.

        The agent works on a throwaway branch so concurrent mutating runs never
        share a working tree. On success its changes are applied back onto the base
        workspace under the shared workspace lock (so the apply itself is
        serialized); on failure or cancellation the worktree is discarded. Any
        setup problem - not a git repo, or a worktree that will not create - falls
        back to a normal in-place run, so isolation can never block a task.
        """
        if not self._should_isolate(agent, payload):
            return await self._run_agent_with_retry(agent, payload)

        manager = GitWorktreeManager(
            payload.workspace,
            root=Path(self._worktree_root) if self._worktree_root else None,
        )
        if not await manager.is_git_repo():
            return await self._run_agent_with_retry(agent, payload)

        if self._best_of_n > 1 and agent.name == "coder":
            return await self._run_best_of_n(agent, payload, manager)

        try:
            wt = await manager.create(f"{agent.name}-{payload.task_id}")
        except WorktreeError:
            logger.warning("worktree: create failed for agent %s; running in-place", agent.name, exc_info=True)
            return await self._run_agent_with_retry(agent, payload)

        isolated_payload = payload.model_copy(update={"workspace": wt.path})
        try:
            result = await self._run_agent_with_retry(agent, isolated_payload)
        except BaseException:
            await self._discard_worktree(manager, wt)
            raise

        integration = await manager.integrate(wt, lock=workspace_lock(payload.workspace))
        await self._emit_integration(payload.task_id, agent.name, integration)
        return result

    async def _run_best_of_n(
        self, agent: Agent, payload: AgentPayload, manager: GitWorktreeManager
    ) -> AgentResult:
        """Run N isolated coder attempts in parallel; integrate only the best (#11).

        Each attempt runs in its own worktree. Candidates are scored deterministically
        (viable > tests-pass > smaller diff); the winner is applied back onto the base
        tree and the losers are discarded. Falls back to a single in-place run when no
        worktree can be created.
        """
        n = self._best_of_n
        pairs: list[tuple[Worktree, AgentPayload]] = []
        for i in range(n):
            try:
                wt = await manager.create(f"{agent.name}-{payload.task_id}-bo{i}")
            except WorktreeError:
                logger.warning("best-of-n: worktree %d create failed; skipping", i, exc_info=True)
                continue
            pairs.append((wt, payload.model_copy(update={"workspace": wt.path})))

        if not pairs:
            return await self._run_agent_with_retry(agent, payload)

        results = await asyncio.gather(
            *[self._run_agent_with_retry(agent, p) for _, p in pairs],
            return_exceptions=True,
        )

        outcomes: list[CandidateOutcome] = []
        for i, ((wt, _), res) in enumerate(zip(pairs, results, strict=False)):
            succeeded = not isinstance(res, BaseException)
            diff_lines = await manager.diff_line_count(wt)
            tests_passed = await self._candidate_tests_pass(wt) if succeeded and diff_lines else None
            outcomes.append(
                CandidateOutcome(
                    index=i,
                    succeeded=succeeded,
                    changed=diff_lines > 0,
                    tests_passed=tests_passed,
                    diff_lines=diff_lines,
                )
            )

        winner = select_best(outcomes)
        await self._stream_manager.emit(
            payload.task_id,
            "best_of_n",
            {"candidates": len(outcomes), "winner": winner, "viable": any_viable(outcomes)},
        )

        winner_result: AgentResult | None = None
        for i, (wt, _) in enumerate(pairs):
            if i == winner and not isinstance(results[i], BaseException):
                integration = await manager.integrate(wt, lock=workspace_lock(payload.workspace))
                await self._emit_integration(payload.task_id, agent.name, integration)
                winner_result = results[i]  # type: ignore[assignment]
            else:
                await self._discard_worktree(manager, wt)

        if winner_result is not None:
            return winner_result
        # No viable winner: surface the first real result, or re-raise the first error.
        first = results[0]
        if isinstance(first, BaseException):
            raise first
        return first  # type: ignore[return-value]

    async def _candidate_tests_pass(self, wt: Worktree) -> bool | None:
        """Run the configured best-of-N test command in *wt*; True/False, or None if unset."""
        cmd = self._best_of_n_test_command
        if not cmd:
            return None
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                cwd=wt.path,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            code = await asyncio.wait_for(proc.wait(), timeout=_BEST_OF_N_TEST_TIMEOUT)
        except (TimeoutError, OSError):
            with contextlib.suppress(ProcessLookupError):
                proc.kill()  # type: ignore[possibly-undefined]
            return False
        return code == 0

    async def _discard_worktree(self, manager: GitWorktreeManager, wt: Worktree) -> None:
        """Best-effort cleanup of an isolated worktree after a failed/cancelled run."""
        try:
            await manager.remove(wt, keep_branch=False)
        except Exception:
            logger.warning("worktree: failed to discard %s", wt.path, exc_info=True)

    async def _emit_integration(self, task_id: str, agent: str, integration: IntegrationResult) -> None:
        """Surface a worktree integration outcome to the UI and, on conflict, the ledger."""
        await self._stream_manager.emit(
            task_id,
            "worktree_integrated",
            {
                "agent": agent,
                "applied": integration.applied,
                "changed": integration.changed,
                "conflicted": integration.conflicted,
                "branch": integration.branch,
            },
        )
        if integration.conflicted:
            logger.warning(
                "worktree: %s changes conflicted on integrate - retained branch %s for manual merge",
                agent,
                integration.branch,
            )
            await self._write_ledger(
                LedgerEntry.new(
                    source=LedgerSource.SYSTEM,
                    task_id=task_id,
                    agent=agent,
                    action="worktree_conflict",
                    output=(
                        f"{agent}'s isolated changes could not be applied cleanly (they overlap other "
                        f"changes). They are preserved on branch '{integration.branch}' for manual merge."
                    ),
                    status=LedgerStatus.COMPLETED,
                )
            )

    async def _run_agent_with_retry(self, agent: Agent, payload: AgentPayload) -> AgentResult:
        """Run an agent, retrying on failure up to the handler's max_retries."""
        task_id = payload.task_id
        await self._stream_manager.emit(
            task_id,
            "agent_started",
            {"agent": agent.name, "task": payload.prompt[:100]},
        )

        while True:
            t0 = time.monotonic()
            try:
                result = await agent.run(payload)
                result.duration_ms = int((time.monotonic() - t0) * 1000)
                self._failure_handler.clear_retry_count(task_id, agent.name)
                await self._task_context_store.update_agent_status(task_id, agent.name, "completed")
                await self._stream_manager.emit(
                    task_id,
                    "agent_completed",
                    {"agent": agent.name, "summary": result.summary, "duration_ms": result.duration_ms},
                )
                return result
            except asyncio.CancelledError:
                self._failure_handler.clear_retry_count(task_id, agent.name)
                raise
            except Exception as exc:
                should_retry = await self._failure_handler.handle_failure(task_id, agent.name, exc)
                if not should_retry:
                    raise
                # The failed attempt may have streamed partial output - tell
                # the UI to discard it before the retry re-streams the answer.
                await self._stream_manager.emit(task_id, "stream_reset", {"agent": agent.name})
                self._maybe_refresh_pools_background()

    def _add_evidence_gate_violations(
        self, agent: Agent, result: AgentResult, violations: list[str]
    ) -> list[str]:
        """Engineering evidence gate (#1): flag code changed with no verification,
        or a code edit that was attempted but never landed.

        When an engineering agent's run mutated code (write_file/patch_file) but ran
        neither the type checker nor a test/command, its "done" is unverified.
        And when it *attempted* a code edit that did not succeed (e.g. the approval
        was denied), no change was applied at all - a "done" claim is simply false.
        Either case is recorded as a violation, routing it through self-repair so the
        agent must actually apply and verify the change before the answer is accepted.
        """
        if agent.name not in ENGINEERING_AGENTS:
            return violations
        succeeded = set(result.successful_tools or [])
        attempted = set(result.tools_used or [])
        if (attempted & _CODE_MUTATING_TOOLS) and not (succeeded & _CODE_MUTATING_TOOLS):
            return [
                *violations,
                "attempted to modify code but the edit did not succeed (it may have been "
                "denied or failed) - no change was applied",
            ]
        if (succeeded & _CODE_MUTATING_TOOLS) and not (succeeded & _CODE_VERIFY_TOOLS):
            return [*violations, "modified code but ran no check_types or test to verify the change"]
        return violations

    async def _verify_agent_claims(
        self, task_id: str, agent: Agent, result: AgentResult, payload: AgentPayload | None = None
    ) -> None:
        """Flag final-answer claims unsupported by tool evidence, repairing first.

        Agents narrate actions ("created the file", "tests pass") the model has no
        way of knowing are true. This cross-checks such claims against the tools
        that actually succeeded. When self-repair is on and a payload is available,
        the agent gets one correction pass to either do the work or drop the claim
        before the (remaining) violations are flagged. Non-fatal: it annotates the
        output and records a ledger entry so a fabricated completion is visible.
        """
        # Only agentic agents report tool evidence (successful_tools is a list,
        # possibly empty); questions/approvals carry no completion claims.
        if result.successful_tools is None or result.requires_approval or result.has_question:
            return
        violations = verify_claims(result.output, result.successful_tools)
        violations = self._add_evidence_gate_violations(agent, result, violations)
        if not violations:
            return

        if self._self_repair and payload is not None:
            violations = await self._attempt_self_repair(task_id, agent, result, payload, violations)
            if not violations:
                return

        bullets = "\n".join(f"- {v}" for v in violations)
        result.output = (
            f"{result.output}\n\n---\n"
            "⚠️ **Unverified claims** - no tool evidence was found for part of this answer:\n"
            f"{bullets}\n\n"
            "Treat the above as not done until confirmed."
        )
        await self._write_ledger(
            LedgerEntry.new(
                source=LedgerSource.SYSTEM,
                task_id=task_id,
                agent=agent.name,
                action="claims_unverified",
                output="; ".join(violations),
                status=LedgerStatus.COMPLETED,
                error_type="unverified_claims",
            )
        )
        await self._stream_manager.emit(
            task_id, "claims_unverified", {"agent": agent.name, "violations": violations}
        )

    async def _attempt_self_repair(
        self, task_id: str, agent: Agent, result: AgentResult, payload: AgentPayload, violations: list[str]
    ) -> list[str]:
        """Give *agent* one pass to fix unsupported claims; return remaining violations.

        Adopts the corrected answer only if it has strictly fewer violations,
        folding its cost/tokens into *result*. On any failure the original answer
        and its violations are kept.
        """
        feedback = (
            "A verification check found claims in your previous answer with no tool evidence:\n"
            + "\n".join(f"- {v}" for v in violations)
            + "\n\nEither actually perform those actions using your tools, or rewrite your answer to "
            "remove any claim you did not verify with a tool. Never state something was done unless a tool did it."
        )
        repair_payload = payload.model_copy(update={"prompt": f"{payload.prompt}\n\n[correction required]\n{feedback}"})
        await self._stream_manager.emit(task_id, "self_repair_started", {"agent": agent.name, "violations": violations})
        try:
            repaired = await agent.run(repair_payload)
        except Exception:
            logger.warning("self-repair: agent %s failed during correction pass", agent.name, exc_info=True)
            return violations
        if repaired.successful_tools is None:
            return violations

        remaining = verify_claims(repaired.output, repaired.successful_tools)
        if len(remaining) >= len(violations):
            return violations  # no improvement - keep the original answer

        result.output = repaired.output
        result.summary = repaired.summary
        result.data = repaired.data
        result.successful_tools = repaired.successful_tools
        result.tools_used = repaired.tools_used
        result.cost_usd += repaired.cost_usd
        result.tokens_in += repaired.tokens_in
        result.tokens_out += repaired.tokens_out
        await self._write_ledger(
            LedgerEntry.new(
                source=LedgerSource.SYSTEM,
                task_id=task_id,
                agent=agent.name,
                action="self_repair",
                output=f"corrected {len(violations) - len(remaining)} unverified claim(s)",
                status=LedgerStatus.COMPLETED,
            )
        )
        await self._stream_manager.emit(task_id, "self_repair_done", {"agent": agent.name, "remaining": remaining})
        return remaining

    async def _critique_result(
        self, task_id: str, agent: Agent, result: AgentResult, payload: AgentPayload | None
    ) -> None:
        """Have a fast reviewer flag an answer that does not address the request.

        Opt-in (settings.critic_enabled). Non-blocking: on a clear gap it appends a
        short reviewer note and records a ``critic_flagged`` entry; it never rewrites
        or retries. Fails open - any error leaves the answer untouched.
        """
        if not self._critic or payload is None or self._tracked_router is None:
            return
        if result.requires_approval or result.has_question or not result.output.strip():
            return
        prompt = _CRITIC_PROMPT.format(request=payload.prompt[:1500], answer=result.output[:3000])
        try:
            response = await self._tracked_router.complete(
                CompletionRequest(
                    prompt=prompt,
                    priority=PoolPriority.MEDIUM,
                    component="critic",
                    task_id=task_id,
                    json_mode=True,
                )
            )
            verdict = json.loads(response.text.strip())
        except Exception:
            logger.debug("critic: review failed for agent %s", agent.name, exc_info=True)
            return

        if verdict.get("adequate", True):
            return
        gap = str(verdict.get("gap", "")).strip()
        if not gap:
            return
        result.output = f"{result.output}\n\n> ⚠️ **Reviewer note:** {gap}"
        await self._write_ledger(
            LedgerEntry.new(
                source=LedgerSource.SYSTEM,
                task_id=task_id,
                agent=agent.name,
                action="critic_flagged",
                output=gap,
                status=LedgerStatus.COMPLETED,
            )
        )
        await self._stream_manager.emit(task_id, "critic_flagged", {"agent": agent.name, "gap": gap})

    async def _handle_agent_result(
        self, task_id: str, agent: Agent, result: AgentResult, payload: AgentPayload | None = None
    ) -> None:
        """Write result to task context, ledger, and notify user if needed."""
        await self._verify_agent_claims(task_id, agent, result, payload)
        await self._critique_result(task_id, agent, result, payload)
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

        await self._write_ledger(
            LedgerEntry.new(
                source=LedgerSource.AGENT,
                task_id=task_id,
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
