"""Main Orchestrator: runs each task on north, the one agent, from start to its ending.

A task goes straight to north. The goal check runs first for an ask that acts
outside north. There is no planner: one used to classify every task with a model
call, about 4 seconds before every answer, to pick the one agent there is.

See docs/CODING_STYLE.md Sections 2.5, 4.1, 6, 10.2, 14.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from typing import Any

from agents import Agent, AgentPayload, AgentResult
from agents.inbox import TaskInbox
from approval import ApprovalDecision, Card, CardType, UserInteraction
from approval.approval_memory import ApprovalMemory
from approval.interaction import APPROVAL_DEFAULT_OPTIONS
from approval.models import DecidedBy
from approval.store import ApprovalStore
from config.strategy import NorthSettings, StrategyMode, describe
from inference.cost_tracker import CostTracker
from inference.exceptions import missing_resource
from ledger import TASK_ENDINGS, LedgerEntry, LedgerFilters, LedgerSource, LedgerStatus, LedgerWriter
from orchestrator.constants import (
    MAX_CONCURRENT_TASKS,
    MAX_QUEUE_BACKOFF_SECONDS,
    POOL_REFRESH_COOLDOWN,
    QUEUE_POLL_INTERVAL_SECONDS,
    STRATEGY_CMD_RE,
)
from orchestrator.exceptions import (
    NorthStarConflictError,
    OrchestratorError,
    TaskCapacityError,
)
from orchestrator.failure_handler import FailureHandler, classify_error
from orchestrator.idempotency import IdempotencyCache, idempotency_key
from orchestrator.journal import TaskJournal
from orchestrator.model_scarcity import (
    MODEL_SCARCITY_MESSAGE as _MODEL_SCARCITY_MESSAGE,
)
from orchestrator.model_scarcity import (
    AgentFailure,
)
from orchestrator.models import (
    TaskRequest,
    TaskResponse,
)
from orchestrator.north_star import NorthStarChecker
from orchestrator.result_audit import ResultAuditor
from orchestrator.running_tasks import RunningTaskStore
from orchestrator.stream import EventStreamManager
from orchestrator.task_intent import execution_profile, is_consequential
from utils.edit_scope import EditAuthorizer
from utils.handoff import ensure_handoff_dir, handoff_dir_for
from utils.ids import generate_id, generate_task_id
from utils.logging import bind_task_id
from utils.tasks import spawn
from utils.time import format_timestamp, utcnow
from utils.tools import ToolDispatchRegistryPort

logger = logging.getLogger(__name__)


def _task_folder(task_id: str, chosen: str) -> str:
    """The folder a task works and writes in: the one chosen for it, else its own (CODING_STYLE §16.1.1).

    Never the folder north was started in. North runs from the home folder, so
    every task without a chosen folder wrote there: a recording landed in `~`.
    """
    return chosen or handoff_dir_for(task_id)


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
        agent: Agent,
        north_star_checker: NorthStarChecker,
        failure_handler: FailureHandler,
        interaction: UserInteraction,
        stream_manager: EventStreamManager,
        approval_store: ApprovalStore,
        north_settings: NorthSettings | None = None,
        tracked_router: CostTracker | None = None,
        tool_registry: ToolDispatchRegistryPort | None = None,
        extraction_pipeline: Any | None = None,
        running_task_store: RunningTaskStore | None = None,
        stuck_task_max_age_seconds: int = 86_400,
        self_repair: bool = True,
        idempotency_window_seconds: int = 60,
        critic: bool = False,
        approval_memory: ApprovalMemory | None = None,
        plan_store: Any | None = None,
        task_inbox: TaskInbox | None = None,
    ) -> None:
        self._ledger = ledger
        # What you send a task while it works, shared with north (CODING_STYLE §15.4).
        self._inbox = task_inbox or TaskInbox()
        # The request each task in flight was started with, to answer a message that arrives after north did.
        self._requests: dict[str, TaskRequest] = {}
        # Everything that reports on a task writes through one journal: durable
        # ledger entry plus the live event, never one without the other.
        self._journal = TaskJournal(ledger=ledger, stream_manager=stream_manager)
        # North, the one agent every task runs on.
        self._agent = agent
        self._north_star_checker = north_star_checker
        self._failure_handler = failure_handler
        self._stream_manager = stream_manager
        self._approval_store = approval_store
        self._north_settings = north_settings
        # The approval layer's one card channel, shared with tools and flows.
        self._interaction = interaction
        self._tracked_router = tracked_router
        # Tasks north ended blocked, so the task's ending row says so (see _finish_task).
        self._blocked_tasks: set[str] = set()
        # What else ends when you cancel a task: the runs it started, such as a flow's test run.
        self._on_cancelled: list[Callable[[str], object]] = []
        # Closes what a task's tools opened (a browser window, say) when the task ends.
        self._tool_registry = tool_registry
        # Task-scoped plan store: north's checklist for the task, cleared when it ends.
        self._plan_store = plan_store
        self._extraction_pipeline = extraction_pipeline
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
                    if existing is not None and existing.status in ("pending", "running", "queued", "retrying"):
                        logger.info("submit_task: deduped duplicate submission to task %s", existing_id)
                        return existing
            if (working := self._working_tasks()) >= MAX_CONCURRENT_TASKS:
                raise TaskCapacityError(
                    f"Too many concurrent tasks ({working} working, "
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
                    agent_output={"workspace": request.workspace},
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
            if self._working_tasks() >= MAX_CONCURRENT_TASKS:
                logger.warning("resume_task: at capacity, leaving task %s registered for a later restart", task_id)
                return False

            await self._journal.write(
                LedgerEntry.new(
                    source=LedgerSource.SYSTEM,
                    task_id=task_id,
                    input=request.prompt,
                    action="task_resumed",
                    agent_output={"workspace": request.workspace},
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
                    if self._working_tasks() >= MAX_CONCURRENT_TASKS:
                        break
                    # If not explicitly woken by model recovery, enforce exponential backoff to prevent burning retries
                    if not wake_signaled and rt.attempt > 0:
                        min_delay = min(poll_interval * (2 ** max(0, rt.attempt - 1)), MAX_QUEUE_BACKOFF_SECONDS)
                        elapsed = (now - rt.heartbeat_at).total_seconds()
                        if elapsed < min_delay:
                            continue
                    async with self._submit_lock:
                        if rt.task_id in self._active_tasks:
                            continue
                        claimed = await self._running_task_store.mark_retrying_from_queued(rt.task_id)
                        if not claimed:
                            continue
                        logger.info("Resuming queued task %s (attempt %d)", rt.task_id, rt.attempt)
                        await self._journal.record(
                            rt.task_id,
                            "task_retrying",
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
        # The durable registry is authoritative for non-terminal live states.
        if self._running_task_store is not None:
            stored_task = await self._running_task_store.get(task_id)
            if stored_task is not None and stored_task.status in ("queued", "retrying", "paused"):
                return TaskResponse(
                    task_id=task_id,
                    status=stored_task.status,
                    created_at=format_timestamp(entries[-1].timestamp),
                )

        for entry in entries:  # query() returns most-recent-first
            terminal = TASK_ENDINGS.get(entry.action)
            if terminal is not None:
                return TaskResponse(task_id=task_id, status=terminal, created_at=format_timestamp(entry.timestamp))
            if entry.action == "task_queued":
                return TaskResponse(task_id=task_id, status="queued", created_at=format_timestamp(entry.timestamp))
            if entry.action == "task_retrying":
                return TaskResponse(task_id=task_id, status="retrying", created_at=format_timestamp(entry.timestamp))
            if entry.action == "task_paused":
                return TaskResponse(task_id=task_id, status="paused", created_at=format_timestamp(entry.timestamp))
            if entry.action == "task_resumed":
                # Running again: the pause or queueing before it no longer holds.
                break
        # No terminal entry yet - the task is still running.
        return TaskResponse(task_id=task_id, status="pending", created_at=format_timestamp(entries[-1].timestamp))

    def when_cancelled(self, callback: Callable[[str], object]) -> None:
        """Call *callback* with a task's id when you cancel it, to end what the task started."""
        self._on_cancelled.append(callback)

    async def cancel_task(self, task_id: str) -> bool:
        """Cancel a running task: stop its pipeline and write a terminal ledger entry.

        Returns False when the task is not in flight (unknown id or already
        finished) - writing a CANCELLED entry then would rewrite the history
        of a completed task, since get_task() reads the most recent entry.
        Runs the task started end with it: a cancelled task's flow test runs
        read "running now" on the Flows page for good (issue #76, finding M16).
        """
        if not await self._stop_task(task_id):
            return False
        for end_what_it_started in self._on_cancelled:
            try:
                end_what_it_started(task_id)
            except Exception:
                logger.warning("Could not end what task %s started", task_id, exc_info=True)
        await self._journal.record(task_id, "task_cancelled", status=LedgerStatus.CANCELLED)
        await self._stream_manager.emit_done(task_id)
        return True

    async def _stop_task(self, task_id: str) -> bool:
        """End a task for good, whatever state it is in; False when there is no such task in flight.

        Stops a running pipeline, and for one that is not running (paused, or
        queued for a model) does the cleanup its pipeline's finally would have
        done: a user-cancelled task must not be recovered on restart, and its
        _process_task finally cannot reliably clear the registry once the task
        is being cancelled (awaits in a cancelling task re-raise CancelledError),
        so the row is dropped here. Shutdown and a crash deliberately leave the
        row so the task is resumed next start. The task's cards are released
        here too: a paused task's cards stayed pending after it was cancelled,
        still counted as waiting on you, and answering them did nothing.
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
        if self._running_task_store is not None:
            await self._running_task_store.clear(task_id)
        self._inbox.close(task_id)
        self._requests.pop(task_id, None)
        self._release_pending_cards(task_id)
        self._close_what_its_tools_opened(task_id)
        return True

    def _close_what_its_tools_opened(self, task_id: str) -> None:
        """Have each tool close what it opened for the task, in the background; the task cannot use it now.

        Its browser tab stayed open until north stopped, a tab for every task (issue #76, W1).
        """
        if self._tool_registry is None:
            return
        try:
            spawn(self._tool_registry.end_task(task_id), name=f"end_task:{task_id}")
        except Exception:  # the rest of the task's cleanup must still run
            logger.warning("Could not close what task %s opened", task_id, exc_info=True)

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
            if self._working_tasks() >= MAX_CONCURRENT_TASKS:
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
        values: dict[str, Any] | None = None,
        reason: str = "",
    ) -> None:
        """Record a user approval decision from the notification callback or Web UI.

        The decision is bound to the server-issued card: task_id and agent are
        taken from the stored card, never from the client, and a card can only
        be resolved while it is pending. *values* are the field values for a
        card that carries work; they are merged against the issued card, so an
        edit can only touch a field north offered as editable.

        Raises:
            LookupError: card_id does not correspond to an issued card.
            ValueError: the card was already resolved.
        """
        card = self._approval_store.get(card_id)
        if card is None:
            raise LookupError(f"Unknown approval card {card_id!r}.")
        if card.status != "pending":
            raise ValueError(f"Approval card {card_id!r} is already resolved ({card.status}).")
        decision, chosen_option = _decision_from_reply(card, decision, chosen_option)

        if not self._approval_store.resolve(
            card_id, decision, chosen_option=chosen_option, values=values, decided_by=DecidedBy.YOU
        ):
            raise ValueError(f"Approval card {card_id!r} could not be resolved.")
        # A card of several questions resolves to all its answers, which the client did not send as one.
        chosen_option = (self._approval_store.get(card_id) or card).chosen_option
        # Which fields the user changed, by name only. Whether the work was
        # accepted as offered or corrected is the useful signal; the values
        # themselves can hold anything the form did, so they go no further than
        # the resolved card. Nothing below writes them to the ledger.
        proposed = card.field_values()
        edited = sorted(name for name, value in card.merge_response(values).items() if value != proposed.get(name))
        await self._learn_your_decision(card, decision, chosen_option, reason=reason, edited=edited)

    async def overrule_approval(self, card_id: str, decision: str, chosen_option: str = "", reason: str = "") -> Card:
        """Replace a decision north took for you with yours, and teach memory.

        Nothing re-runs: what north already did, or refused, stays done. Memory
        learns your decision exactly as it learns an answer you gave on a
        waiting card, so next time north decides your way (CODING_STYLE §7.3).

        Raises:
            LookupError: card_id does not correspond to an issued card.
            ValueError: the card is not a decision north took, or *decision*
                does not fit it.
        """
        card = self._approval_store.get(card_id)
        if card is None:
            raise LookupError(f"Unknown approval card {card_id!r}.")
        overruled = self._approval_store.overrule(card_id, decision, chosen_option=chosen_option, reason=reason)
        await self._learn_your_decision(card, decision, chosen_option, reason=reason, overruled=True)
        return overruled

    async def _learn_your_decision(
        self,
        card: Card,
        decision: str,
        chosen_option: str,
        *,
        reason: str = "",
        edited: list[str] | None = None,
        overruled: bool = False,
    ) -> None:
        """Record a decision of yours where north learns from it. The one path answers and overrules share."""
        edited = edited or []
        # One row per decision, before anything else: replay reads it under the
        # card's action key (what the policy recalls by), and flows learn your
        # taste from it when the card is prepared work.
        if self._approval_memory is not None:
            action_key = card.action_key if card.type == CardType.APPROVAL else ""
            self._approval_memory.record(
                card.agent,
                action_key,
                decision,
                card=card,
                chosen_option=chosen_option,
                reason=reason,
                edited_fields=edited,
            )

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
                else f"card_id={card.id}"
            )
        event = "approval_overruled" if overruled else "approval_responded"
        await self._journal.record(
            card.task_id,
            action,
            status=status,
            source=source,
            agent=card.agent,
            input=ledger_input,
            output=f"chosen_option={chosen_option or decision}",
            event=event,
            payload={
                "card_id": card.id,
                "decision": decision,
                "chosen_option": chosen_option,
                "edited_fields": edited,
                "overruled": overruled,
            },
            agent_output={
                "card_id": card.id,
                "decision": decision,
                "chosen_option": chosen_option,
                "edited_fields": edited,
                "reason": reason,
                "source": card.source,
                "overruled": overruled,
            },
        )

    async def send_message(self, task_id: str, text: str) -> bool:
        """Send *text* into a task that has not finished (CODING_STYLE §15.4); False when there is none.

        Its agents read it before their next step and before answering. A task
        paused, or queued for a model, reads it when it runs again; the ledger
        entry is what a task that starts over after a restart reads it from.
        """
        if task_id not in self._active_tasks and (
            self._running_task_store is None or await self._running_task_store.get(task_id) is None
        ):
            return False
        self._inbox.post(task_id, text)
        await self._journal.record(
            task_id,
            "user_message",
            source=LedgerSource.PROMPT,
            agent="user",
            input=text,
            payload={"text": text, "timestamp": format_timestamp(utcnow())},
        )
        return True

    @property
    def active_task_ids(self) -> frozenset[str]:
        """Ids of tasks currently in flight (asyncio tasks still running)."""
        return frozenset(self._active_tasks)

    def is_waiting_on_you(self, task_id: str) -> bool:
        """Whether *task_id* is paused on a card only you can answer - not working, not stuck."""
        return task_id in self._tasks_waiting_on_you()

    def _working_tasks(self) -> int:
        """In-flight tasks that count against the cap.

        A task paused on your answer holds no slot: cards never expire
        (CODING_STYLE §13.5), so enough of them would otherwise fill the cap.
        """
        return len(self._active_tasks.keys() - self._tasks_waiting_on_you())

    def _tasks_waiting_on_you(self) -> set[str]:
        return self._approval_store.tasks_waiting_on_you() if self._approval_store is not None else set()

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
        if not await self._stop_task(task_id):
            return False
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
        await self._journal.record(task_id, "agent_completed", agent="orchestrator", output=msg, event="task_completed")
        await self._stream_manager.emit_done(task_id)
        return True

    async def _process_task(self, task_id: str, request: TaskRequest) -> None:
        """Full pipeline: the goal check when the ask acts outside north, then north itself."""
        bind_task_id(task_id)  # attach correlation ID to every log line in this context
        # Agents are told this directory exists before they are asked to use it.
        # Creating it lazily, wherever a write happened to land first, meant a
        # pipeline that starts by *reading* found nothing and stopped the task.
        await asyncio.to_thread(ensure_handoff_dir, task_id)
        task_start = time.monotonic()
        self._requests[task_id] = request
        await self._open_inbox(task_id)
        # Running again after a pause: its stream was closed, and what it streamed before is replaced.
        if self._stream_manager.reopen(task_id):
            await self._stream_manager.emit(task_id, "stream_reset", {})
        try:
            await self._run_pipeline(task_id, request)
        except asyncio.CancelledError:
            # cancel_task() already wrote the ledger entry and emitted events.
            raise
        except NorthStarConflictError as e:
            await self._reject_conflicting_task(task_id, task_start, e)
        except Exception as e:
            await self._report_task_failure(task_id, task_start, e)
        finally:
            await self._release_finished_task(task_id)

    async def _open_inbox(self, task_id: str) -> None:
        """Take messages for the task; one starting over opens with those you sent before."""
        if self._inbox.is_open(task_id):
            return
        entries = await self._ledger.query(LedgerFilters(task_id=task_id, agent="user", order_asc=True, limit=500))
        self._inbox.open(task_id, [entry.input for entry in entries if entry.action == "user_message" and entry.input])

    async def _run_pipeline(self, task_id: str, request: TaskRequest) -> None:
        # Strategy command shortcut - handle before full pipeline
        if await self._handle_strategy_command(task_id, request.prompt):
            return
        context = self._with_earlier_decisions(task_id, request.context)
        await self._stage_north_star(task_id, request.prompt, context)
        await self._run_north(
            task_id,
            request.prompt,
            _task_folder(task_id, request.workspace),
            context,
            request.edit_scope,
            profile=execution_profile(request.prompt, request.workspace),
        )

    def _with_earlier_decisions(self, task_id: str, context: str) -> str:
        """*context* plus what you already decided in this task, when it is running again.

        A task retried after a model outage starts over. The new run asked again
        which Chrome profile to use, after you had approved one, because nothing
        told it you had. Decisions North made for you are listed too; a rejection
        matters as much as an approval.
        """
        decided = self._approval_store.decided_for_task(task_id) if self._approval_store is not None else []
        if not decided:
            return context
        lines = [
            f'- Asked "{card.message[:200]}" and the user answered: {card.chosen_option}'
            if card.status == ApprovalDecision.ANSWERED
            else f"- {card.status.capitalize()}: {card.title} - {card.message[:200]}"
            for card in decided
        ]
        block = (
            "## Already decided in this task\n"
            "This task started over (for example after a model outage). These were already decided; "
            "continue from them and do not ask again:\n" + "\n".join(lines)
        )
        return f"{context}\n\n{block}" if context else block

    async def _reject_conflicting_task(self, task_id: str, task_start: float, error: Exception) -> None:
        logger.warning("Task %s rejected: conflicts with North Star goals", task_id)
        await self._stream_manager.emit(task_id, "task_rejected", {"reason": str(error)})
        await self._record_task_failure(task_id, task_start, str(error), LedgerStatus.CANCELLED, "north_star_conflict")
        await self._stream_manager.emit_done(task_id)

    async def _report_task_failure(self, task_id: str, task_start: float, error: Exception) -> None:
        error_type = classify_error(error)
        if not missing_resource(error):
            logger.error("Task %s failed: %s", task_id, error, exc_info=True)
            await self._stream_manager.emit(task_id, "task_failed", {"error": str(error), "error_type": error_type})
            await self._record_task_failure(task_id, task_start, str(error), LedgerStatus.FAILED, error_type)
            await self._stream_manager.emit_done(task_id)
            return

        acted = await self._has_acted(task_id)
        if not acted and await self._queue_for_model_recovery(task_id, error):
            return

        reason = (
            f"{_MODEL_SCARCITY_MESSAGE}. This task had already acted, so north stopped rather than run it "
            "again and repeat what it did. Send it again if you want it re-run."
            if acted
            else f"{_MODEL_SCARCITY_MESSAGE}; no queue to wait in"
        )
        logger.error("Task %s needs attention: %s: %s", task_id, reason, error)
        await self._record_task_needs_attention(
            task_id,
            reason,
            error_type=error_type,
            duration_ms=int((time.monotonic() - task_start) * 1000),
        )

    async def _has_acted(self, task_id: str) -> bool:
        """Whether *task_id* has begun to change something, so running it again could repeat that."""
        if self._running_task_store is None:
            return False
        running = await self._running_task_store.get(task_id)
        return running is not None and running.has_side_effects

    async def _queue_for_model_recovery(self, task_id: str, error: Exception) -> bool:
        """Queue the task to run again when the model or network is back. False only without a queue.

        A missing resource freezes work; it never uses up attempts (CODING_STYLE
        §13.5, #34). The count is kept for the backoff between tries and for you.
        """
        if self._running_task_store is None:
            return False
        attempt = await self._attempts_so_far(task_id) + 1
        logger.warning(
            "Task %s queued for model availability recovery (attempt %d): %s",
            task_id,
            attempt,
            error,
        )
        if not await self._running_task_store.mark_queued(task_id, attempt=attempt):
            return False
        await self._journal.record(
            task_id,
            "task_queued",
            status=LedgerStatus.PENDING,
            output=f"model pool unavailable - queued for retry (attempt {attempt})",
            payload={
                "reason": "model pool unavailable - queued for retry",
                "attempt": attempt,
            },
        )
        return True

    async def _attempts_so_far(self, task_id: str) -> int:
        if self._running_task_store is None:
            return 0
        stored_task = await self._running_task_store.get(task_id)
        return stored_task.attempt if stored_task is not None else 0

    async def _release_finished_task(self, task_id: str) -> None:
        """Drop everything a task holds once it can no longer act, whatever ended it."""
        stored_task = None
        if self._running_task_store is not None:
            with contextlib.suppress(Exception, asyncio.CancelledError):
                stored_task = await asyncio.shield(self._running_task_store.get(task_id))
        is_resumable = stored_task is not None and stored_task.status in {"queued", "paused"}
        if is_resumable:
            return
        self._close_what_its_tools_opened(task_id)
        self._inbox.close(task_id)
        self._requests.pop(task_id, None)
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
        # The task has reached a terminal state, so it is no longer in-flight.
        if self._running_task_store is not None:
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await asyncio.shield(self._running_task_store.clear(task_id))

    async def _handle_alignment_conflict(self, task_id: str, tension: str) -> None:
        """Prompt user for approval when a North Star conflict is detected.

        The north_star_conflict SSE event is emitted first for UI awareness, then
        the card is routed through the shared UserInteraction mediator so the
        approval policy and notifier apply to it as to every other card.
        """
        card = Card.new(
            type=CardType.APPROVAL,
            task_id=task_id,
            agent="orchestrator",
            title="North Star Conflict Detected",
            message=tension or "This task conflicts with one of your active goals. Proceed?",
            options=["Proceed anyway", "Cancel"],
            rejecting=["Cancel"],
        )
        # Emit the conflict event before surfacing so the UI can show context.
        await self._stream_manager.emit(task_id, "north_star_conflict", {"tension": tension})
        # The shared mediator registers the card, lets the approval policy rule on
        # it, fires the notifier, and blocks until the user responds.
        decided = await self._interaction.request_decision(card)
        if decided.status != ApprovalDecision.APPROVED:
            raise NorthStarConflictError(tension or "North Star conflict")

    async def _stage_north_star(self, task_id: str, prompt: str, context: str) -> None:
        """The goal check, for an ask that acts outside north (CODING_STYLE §7).

        Whether it does is read from the ask's words (`task_intent.is_consequential`);
        a model judges only whether it fits your goals.
        """
        if not is_consequential(prompt, context):
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

    async def _record_task_needs_attention(
        self,
        task_id: str,
        reason: str,
        *,
        error_type: str,
        duration_ms: int | None = None,
    ) -> None:
        """Park exhausted work visibly instead of burying it as a completed skip."""
        task_cost_usd = self._tracked_router.pop_task_cost(task_id) if self._tracked_router else 0.0
        await self._journal.write(
            LedgerEntry.new(
                source=LedgerSource.SYSTEM,
                task_id=task_id,
                action="task_needs_attention",
                output=reason,
                status=LedgerStatus.FAILED,
                error_type=error_type,
                duration_ms=duration_ms,
                cost_usd=task_cost_usd,
            )
        )
        await self._stream_manager.emit(
            task_id,
            "task_needs_attention",
            {"reason": reason, "error_type": error_type, "cost_usd": task_cost_usd},
        )
        await self._stream_manager.emit_done(task_id)

    async def _run_north(
        self,
        task_id: str,
        prompt: str,
        workspace: str,
        context: str,
        edit_scope: EditAuthorizer | None,
        *,
        profile: str = "standard",
    ) -> None:
        """Run the task on north, then write how it ended."""
        await self._heartbeat(task_id)
        await self._stream_manager.emit(task_id, "executing", {"agents": [self._agent.name]})
        failure = await self._run_agent(task_id, prompt, workspace, context, profile, edit_scope)
        await self._finish_task(task_id, failure=failure)

    async def _finish_task(self, task_id: str, *, failure: AgentFailure | None = None) -> None:
        """Write the terminal ledger entry and emit done events.

        A model that was not available queues the task to run again; any other
        failure ends it FAILED, with the reason.
        """
        request = self._requests.get(task_id)
        scarcity = failure is not None and failure.error_type == "model_unavailable"
        if not scarcity and request is not None and self._inbox.take_unread(task_id):
            await self._answer_late_messages(task_id, request)
            return
        if scarcity:
            unavailable = RuntimeError(_MODEL_SCARCITY_MESSAGE)
            if await self._queue_for_model_recovery(task_id, unavailable):
                return
            await self._record_task_needs_attention(
                task_id,
                f"{unavailable}; no queue to wait in",
                error_type="model_unavailable",
            )
            return
        task_cost_usd = self._tracked_router.pop_task_cost(task_id) if self._tracked_router else 0.0
        if failure is not None:
            action, status, err = "task_failed", LedgerStatus.FAILED, failure.error_type or "agent_failure"
        else:
            action, status, err = "task_completed", LedgerStatus.COMPLETED, None
        # The work ended but what was asked did not happen: the Tasks page listed these as
        # "completed" and counted them as finished work while the chat said BLOCKED (finding P11).
        if task_id in self._blocked_tasks and err is None:
            err = "blocked"
        self._blocked_tasks.discard(task_id)
        await self._journal.write(
            LedgerEntry.new(
                source=LedgerSource.SYSTEM,
                task_id=task_id,
                action=action,
                output=failure.reason if failure is not None else None,
                status=status,
                error_type=err,
                cost_usd=task_cost_usd,
            )
        )
        if failure is not None:
            await self._stream_manager.emit(
                task_id,
                "task_failed",
                {"error": failure.reason, "error_type": err, "cost_usd": task_cost_usd},
            )
        else:
            await self._stream_manager.emit(task_id, "task_completed", {"cost_usd": task_cost_usd})
        await self._stream_manager.emit_done(task_id)
        # Trigger extraction immediately after a task so preferences stated
        # mid-task land in judgement_rules.md before the next task starts.
        if self._extraction_pipeline is not None:
            spawn(self._extraction_pipeline.run_once(), name="extraction_run_once")

    async def _answer_late_messages(self, task_id: str, request: TaskRequest) -> None:
        """Answer, in this task, messages you sent after north had answered (CODING_STYLE §15.4).

        North reads every message from the inbox and is told what the task already
        did and said, so it acts on what is new instead of starting over.
        """
        entries = await self._ledger.query(LedgerFilters(task_id=task_id, order_asc=True, limit=500))
        answered = "\n\n".join(
            f"{entry.agent}: {entry.output}" for entry in entries if entry.action in _ANSWER_ACTIONS and entry.output
        )
        context = (
            f"{self._with_earlier_decisions(task_id, request.context)}\n\n"
            f"## Already done in this task\n{answered or 'Nothing was answered yet.'}\n\n"
            "The user sent new messages while this task worked; they follow. "
            "Act on them, and do not repeat what is already done. Your answer replaces the one above, "
            "so give the whole answer, keeping what still holds."
        ).strip()
        # Live views show the answer being written; the one above is about to be replaced.
        await self._stream_manager.emit(task_id, "stream_reset", {})
        await self._run_north(
            task_id, request.prompt, _task_folder(task_id, request.workspace), context, request.edit_scope
        )

    async def _run_agent(
        self,
        task_id: str,
        prompt: str,
        workspace: str,
        context: str,
        profile: str,
        edit_scope: EditAuthorizer | None,
    ) -> AgentFailure | None:
        """Run north on the task; how it failed after every retry, or None when it answered.

        ``edit_scope`` is the task's server-owned :class:`EditAuthorizer`, stamped
        onto the payload so mutating file tools enforce it.
        """
        await self._heartbeat(task_id)
        payload = AgentPayload(
            task_id=task_id,
            prompt=prompt,
            workspace=workspace,
            # The task's own workspace, from the request - the server grants it.
            granted_workspace=workspace or "",
            context=context,
            execution_profile=profile,
            edit_scope=edit_scope,
        )
        try:
            result = await self._run_agent_with_retry(self._agent, payload)
        except asyncio.CancelledError:
            # A cancelled task means cancel_task() was called - propagate
            # so the outer _process_task handler can write the ledger entry.
            raise
        except Exception as exc:
            error_type = classify_error(exc)
            logger.error("north failed in task '%s' - error_type=%s: %s", task_id, error_type, exc, exc_info=exc)
            await self._journal.write(
                LedgerEntry.new(
                    source=LedgerSource.AGENT,
                    task_id=task_id,
                    agent=self._agent.name,
                    action="agent_execution_failed",
                    output=str(exc),
                    status=LedgerStatus.FAILED,
                    error_type=error_type,
                )
            )
            return AgentFailure(str(exc), error_type)
        await self._handle_agent_result(task_id, self._agent, result, payload)
        return None

    def _maybe_refresh_catalog_background(self) -> None:
        if self._tracked_router is None:
            return
        now = time.monotonic()
        if now - self._last_pool_refresh_at < POOL_REFRESH_COOLDOWN:
            return
        self._last_pool_refresh_at = now

        async def _refresh() -> None:
            try:
                await self._tracked_router.refresh_catalog()  # type: ignore[union-attr]
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
                if not should_retry or await self._has_acted(task_id):  # a new attempt would do it all again
                    self._failure_handler.clear_retry_count(task_id, agent.name)
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
                self._maybe_refresh_catalog_background()

    async def _handle_agent_result(
        self, task_id: str, agent: Agent, result: AgentResult, payload: AgentPayload | None = None
    ) -> None:
        """Write the result to the ledger, and tell you, or ask you, as it needs."""
        await self._auditor.audit(task_id, agent, result, payload)
        # Verification/self-repair can refine the result after Agent.run() first
        # persisted it. Keep the run index aligned with the final audited output.
        if result.run_id and agent.deps.agent_run_store is not None:
            await agent.deps.agent_run_store.complete(result.run_id, result)
        if isinstance(result.data, dict) and result.data.get("outcome_status") == "blocked":
            self._blocked_tasks.add(task_id)

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
                cached_tokens=result.cached_tokens,
                cache_missed_tokens=result.cache_missed_tokens,
                cache_miss_count=result.cache_miss_count,
                model_used=", ".join(result.models_used) or None,
            )
        )

        if result.requires_approval or result.has_question:
            card_type = CardType.QUESTION if result.has_question else CardType.APPROVAL
            card = Card.new(
                type=card_type,
                task_id=task_id,
                agent=agent.name,
                title=f"{agent.name.capitalize()} - Action Required",
                message=result.question or result.output,
                options=result.question_options if result.has_question else ["Approve", "Reject"],
            )
            await self._interaction.notify(card)
        else:
            await self._interaction.inform(
                task_id=task_id,
                agent=agent.name,
                title=f"{agent.name.capitalize()} - Done",
                message=result.summary,
            )


# The ledger action that carries north's answer to the task.
_ANSWER_ACTIONS = frozenset({"agent_completed"})
_DECISIONS = frozenset(ApprovalDecision)
_PLAIN_YES_NO = frozenset(APPROVAL_DEFAULT_OPTIONS)


def _decision_from_reply(card: Card, decision: str, chosen_option: str) -> tuple[str, str]:
    """The decision a reply means, as one of `ApprovalDecision`, and the option it chose.

    The web UI sends a decision ("approved") and the option. A Telegram button sends only the option it
    shows ("Approve"), and stored as the decision that read as "not approved": a tap on Approve was a no.
    An option of the card is read here; anything else is refused rather than stored. What an option
    means is what the card declared when it was made, never what its words sound like.
    """
    if decision in _DECISIONS:
        answers_an_option = decision in (ApprovalDecision.APPROVED, ApprovalDecision.REJECTED)
        no_with_a_reason = decision == ApprovalDecision.REJECTED and chosen_option  # the TUI's "Reject: why"
        if (
            answers_an_option
            and set(card.options) - _PLAIN_YES_NO
            and chosen_option not in card.options
            and not no_with_a_reason
        ):
            # A Resume/Discard card answered with a bare Approve closed and did nothing.
            raise ValueError(f"Card {card.id!r} needs one of its own options: {', '.join(card.options)}.")
        return decision, chosen_option
    option = next((o for o in card.options if o.lower() == decision.strip().lower()), None)
    if option is None:
        raise ValueError(f"{decision!r} is not a decision or an option of card {card.id!r}.")
    if card.type is CardType.QUESTION:
        return ApprovalDecision.ANSWERED, option
    return (ApprovalDecision.REJECTED if option in card.rejecting else ApprovalDecision.APPROVED), option
