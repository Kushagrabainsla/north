"""Main Orchestrator — ties Stages 1–4.

See docs/CODING_STYLE.md Sections 2.5, 4.1, 6, 10.2, 14.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from agents import Agent, AgentPayload, AgentResult
from agents.registry import AgentRegistry
from approval import ApprovalDecision, Card, CardType, JudgementFilter, Notifier
from approval.store import ApprovalStore
from config.strategy import NorthSettings, StrategyMode, describe
from inference.cost_tracker import CostTracker
from inference.models import CompletionRequest, PoolPriority
from ledger import LedgerEntry, LedgerFilters, LedgerSource, LedgerStatus, LedgerWriter
from orchestrator.constants import (
    MAX_CONCURRENT_TASKS,
    NORTH_STAR_CONFIDENCE_THRESHOLD,
    POOL_REFRESH_COOLDOWN,
    STRATEGY_CMD_RE,
)
from orchestrator.exceptions import NorthStarConflictError, OrchestratorError, TaskCapacityError
from orchestrator.failure_handler import FailureHandler, classify_error
from orchestrator.models import (
    ExecutionMode,
    ExecutionPlan,
    IntentClassification,
    TaskRequest,
    TaskResponse,
)
from orchestrator.north_star import NorthStarChecker
from orchestrator.router import ExecutionPlanner
from orchestrator.stream import EventStreamManager
from orchestrator.synthesizer import ResultSynthesizer
from orchestrator.task_context import TaskContextStore
from orchestrator.verification import verify_claims
from tools.exceptions import ToolNotFoundError
from tools.models import ToolInput
from tools.registry import ToolRegistry
from utils.ids import generate_id, generate_task_id
from utils.logging import bind_task_id
from utils.time import format_timestamp, utcnow

logger = logging.getLogger(__name__)


def _on_extraction_done(t: asyncio.Task) -> None:
    if t.cancelled():
        logger.warning("post-task extraction cancelled (shutdown during extraction?)")
    elif t.exception() is not None:
        logger.warning("post-task extraction failed: %s", t.exception())


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
    ) -> None:
        self._ledger = ledger
        self._agent_registry = agent_registry
        self._north_star_checker = north_star_checker
        self._execution_planner = execution_planner
        self._task_context_store = task_context_store
        self._failure_handler = failure_handler
        self._notifier = notifier
        self._stream_manager = stream_manager
        self._approval_store = approval_store
        self._judgement_filter = judgement_filter
        self._north_settings = north_settings
        self._synthesizer = synthesizer
        self._tracked_router = tracked_router
        self._episodic_store = episodic_store
        self._tool_registry = tool_registry
        self._default_workspace = default_workspace
        self._extraction_pipeline = extraction_pipeline
        # Maps task_id → running asyncio.Task so cancel_task() can stop it.
        self._active_tasks: dict[str, asyncio.Task] = {}
        # Makes the capacity check-then-register in submit_task atomic — without
        # it, concurrent submissions could all pass the check before any of them
        # registers, bypassing MAX_CONCURRENT_TASKS.
        self._submit_lock = asyncio.Lock()
        # Holds references to short-lived fire-and-forget tasks (episode recording,
        # etc.) so they are not garbage-collected before they finish.
        self._background_tasks: set[asyncio.Task] = set()
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
            task = asyncio.create_task(self._process_task(task_id, request))
            self._active_tasks[task_id] = task
            task.add_done_callback(lambda _: self._active_tasks.pop(task_id, None))

        return TaskResponse(
            task_id=task_id,
            status=LedgerStatus.PENDING.value,
            created_at=format_timestamp(now),
        )

    async def get_task(self, task_id: str) -> TaskResponse | None:
        """Return the current status of a task by reading its most recent ledger entry."""
        # Relies on LedgerWriter.query() returning entries ORDER BY timestamp DESC (see base.py).
        entries = await self._ledger.query(LedgerFilters(task_id=task_id, limit=1))
        if not entries:
            return None
        entry = entries[0]
        return TaskResponse(
            task_id=task_id,
            status=entry.status.value if entry.status else "unknown",
            created_at=format_timestamp(entry.timestamp),
        )

    async def cancel_task(self, task_id: str) -> bool:
        """Cancel a running task: stop its pipeline and write a terminal ledger entry.

        Returns False when the task is not in flight (unknown id or already
        finished) — writing a CANCELLED entry then would rewrite the history
        of a completed task, since get_task() reads the most recent entry.
        """
        running = self._active_tasks.pop(task_id, None)
        if running is None:
            return False
        if not running.done():
            running.cancel()
        if self._tracked_router:
            self._tracked_router.pop_task_cost(task_id)
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

        status = LedgerStatus.APPROVED if decision == ApprovalDecision.APPROVED else LedgerStatus.REJECTED
        # Include the card message so the extraction pipeline can learn a
        # meaningful preference fact (e.g. "User always approves X from agent Y").
        # Without this, the input would just be an opaque card_id.
        ledger_input = (
            f"question: {card.message}\noptions: {', '.join(card.options)}" if card.message else f"card_id={card_id}"
        )
        await self._write_ledger(
            LedgerEntry.new(
                source=LedgerSource.APPROVAL,
                task_id=card.task_id,
                agent=card.agent,
                action=f"approval_responded: {decision}",
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

    async def list_active_tasks(self) -> list[TaskResponse]:
        """Returns tasks that are currently in-flight (asyncio tasks still running)."""
        results = []
        for task_id in list(self._active_tasks):
            resp = await self.get_task(task_id)
            if resp is not None:
                results.append(resp)
        return results

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

    # ------------------------------------------------------------------ #
    #  Notification with judgement filtering                               #
    # ------------------------------------------------------------------ #

    async def _notify(self, card: Card) -> None:
        """Register card, run judgement filter, then auto-resolve or surface to user.

        Always registers the card in the ApprovalStore first so it is wait-able
        regardless of which code path follows.  The Notifier implementations
        are responsible only for delivering the alert — they never touch the store.
        """
        self._approval_store.add(card)
        if self._judgement_filter is not None:
            decision, chosen_option = await self._judgement_filter.check(card)
            if decision is not None:
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
                self._approval_store.resolve(card.id, decision, chosen_option=chosen_option or "")
                return
        await self._notifier.notify(card)

    # ------------------------------------------------------------------ #
    #  Stage pipeline                                                      #
    # ------------------------------------------------------------------ #

    def _detect_strategy_command(self, prompt: str) -> StrategyMode | None:
        """Return a StrategyMode if the prompt is an unambiguous strategy command.

        Requires the prompt to be *only* a strategy directive — no surrounding
        prose — so incidental mentions ("I was in sport mode") never mutate
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
            # Strategy command shortcut — handle before full pipeline
            if await self._handle_strategy_command(task_id, request.prompt):
                return

            classification, plan = await self._stage_plan(task_id, request.prompt)
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
            duration_ms = int((time.monotonic() - task_start) * 1000)
            task_cost_usd = self._tracked_router.pop_task_cost(task_id) if self._tracked_router else 0.0
            await self._write_ledger(
                LedgerEntry.new(
                    source=LedgerSource.SYSTEM,
                    task_id=task_id,
                    action="task_cancelled",
                    output=str(e),
                    status=LedgerStatus.CANCELLED,
                    duration_ms=duration_ms,
                    cost_usd=task_cost_usd,
                )
            )
            await self._stream_manager.emit(task_id, "task_cancelled", {"reason": str(e)})
            await self._stream_manager.emit_done(task_id)
        except Exception as e:
            duration_ms = int((time.monotonic() - task_start) * 1000)
            task_cost_usd = self._tracked_router.pop_task_cost(task_id) if self._tracked_router else 0.0
            error_type = classify_error(e)
            logger.exception(
                "Unhandled error processing task %s — error_type=%s duration_ms=%d: %s",
                task_id,
                error_type,
                duration_ms,
                e,
            )
            await self._write_ledger(
                LedgerEntry.new(
                    source=LedgerSource.SYSTEM,
                    task_id=task_id,
                    action="task_failed",
                    output=str(e),
                    status=LedgerStatus.FAILED,
                    duration_ms=duration_ms,
                    error_type=error_type,
                    cost_usd=task_cost_usd,
                )
            )
            await self._stream_manager.emit(task_id, "task_failed", {"error": str(e), "error_type": error_type})
            await self._stream_manager.emit_done(task_id)
        finally:
            # Reap this task's tracked cost exactly once, regardless of exit path.
            # The success/failure/conflict/cancel paths already pop it to record
            # the cost in the ledger; popping again here is a no-op (pop_task_cost
            # returns 0.0 when absent), so this only catches tasks that recorded
            # cost but never reached one of those pops — preventing an unbounded
            # leak in CostTracker._task_costs on a long-lived server.
            if self._tracked_router is not None:
                self._tracked_router.pop_task_cost(task_id)

    async def _stage_plan(self, task_id: str, prompt: str) -> tuple[IntentClassification, ExecutionPlan]:
        """Stages 1+3: Classify intent and build execution plan in one LLM call."""
        await self._stream_manager.emit(task_id, "classifying", {"prompt": prompt})

        classification, plan = await self._execution_planner.plan_all(prompt, task_id=task_id)

        await self._write_ledger(
            LedgerEntry.new(
                source=LedgerSource.SYSTEM,
                task_id=task_id,
                action=f"classified_as_{'consequential' if classification.is_consequential else 'trivial'}",
                output=classification.reasoning,
                status=LedgerStatus.COMPLETED,
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

        The north_star_conflict SSE event is emitted first for UI awareness,
        then the card is routed through _notify() so the JudgementFilter and
        Notifier are applied consistently with every other approval card.
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
        # Emit the conflict event before notifying so the UI can show context.
        await self._stream_manager.emit(task_id, "north_star_conflict", {"tension": tension})
        # _notify() registers the card in the ApprovalStore, applies the
        # JudgementFilter (auto-resolve if rules match), and fires the Notifier.
        await self._notify(card)
        timeout = self._north_settings.approval_timeout_seconds if self._north_settings else 300.0
        current = await self._approval_store.wait_for_decision(card.id, timeout=timeout)
        if current is None:
            logger.warning(
                "North Star approval timed out for task %s — treating as rejection",
                task_id,
            )
            # Resolve so the card doesn't linger as "pending" in the store forever.
            self._approval_store.resolve(card.id, ApprovalDecision.TIMEOUT_REJECTED)
            raise NorthStarConflictError(tension or "North Star conflict (approval timed out)")
        # An explicit option choice wins; otherwise fall back to the decision
        # status — the JudgementFilter and plain approve/reject buttons resolve
        # with a status only, no chosen_option.
        chosen_opt = (current.chosen_option or "").strip().lower()
        approved = chosen_opt == card.options[0].lower() if chosen_opt else current.status == ApprovalDecision.APPROVED
        if not approved:
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
        # borderline tasks (e.g. "schedule a reminder" — local? external?).
        if classification.confidence < NORTH_STAR_CONFIDENCE_THRESHOLD:
            logger.info(
                "Skipping north star check for task %s — classifier confidence %.2f < %.2f threshold",
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
            logger.warning("North Star check failed — blocking task (fail closed): %s", e)
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
                f"North Star alignment could not be evaluated — task blocked (fail closed): {e}"
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
            "Skipping agent '%s' in task %s — dependencies failed: %s",
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
                output=f"Skipped — dependencies failed: {', '.join(failed_deps)}",
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
        Agents without declared dependencies always run.
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
        return all_failures

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

        t = asyncio.create_task(self._record_episode(task_id, prompt, plan.agents, domain))
        self._background_tasks.add(t)
        t.add_done_callback(self._background_tasks.discard)
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
        # Single-tool tasks (deterministic, no agent reasoning) are skipped —
        # they produce no signal worth extracting.
        if self._extraction_pipeline is not None and not skip_extraction:
            t = asyncio.create_task(self._extraction_pipeline.run_once())
            t.add_done_callback(_on_extraction_done)

    async def _record_episode(self, task_id: str, prompt: str, agents: list[str], domain: str) -> None:
        """Write a task episode to episodic memory after completion."""
        if self._episodic_store is None:
            return
        all_data = await self._task_context_store.get_all(task_id)
        outputs = [(all_data.get(agent) or {}).get("output", "") for agent in agents]
        combined = "\n".join(o for o in outputs if o).strip()
        if not combined:
            return
        summary = await self._summarize_episode(task_id, prompt, combined)
        try:
            await self._episodic_store.record(task_id=task_id, domain=domain, summary=summary)
        except Exception:
            logger.warning("Episodic record failed for task %s", task_id, exc_info=True)

    async def _summarize_episode(self, task_id: str, prompt: str, output: str) -> str:
        """Generate a retrieval-friendly episode summary via the LLM.

        Falls back to a plain truncated string when the cost tracker is
        unavailable (tests, offline mode) so episodic memory always gets
        *something* rather than nothing.
        """
        fallback = f"Task: {prompt[:200]}\nResult: {output[:500]}"
        if self._tracked_router is None:
            return fallback
        try:
            response = await self._tracked_router.complete(  # CostTracker is also an InferenceRouter
                CompletionRequest(
                    prompt=(
                        "Summarize this completed AI task in 2–3 sentences for future retrieval. "
                        "Include what was requested, what was done, and any key outcomes or decisions.\n\n"
                        f"Task: {prompt}\n\nResult: {output[:3000]}"
                    ),
                    priority=PoolPriority.LOW,
                    component="episodic_summary",
                    task_id=None,  # task already completed; task_id cost was already popped
                )
            )
            text = response.text.strip()
            # Guard/classifier models sometimes return a bare float score instead
            # of prose. Detect and discard those so episodic memory stays readable.
            try:
                float(text)
                logger.warning(
                    "Episode summarization returned a numeric score for task %s (model=%s) — using fallback",
                    task_id,
                    response.model_used,
                )
                return fallback
            except ValueError:
                pass
            return text or fallback
        except Exception:
            logger.warning("Episode summarization failed for task %s", task_id, exc_info=True)
            return fallback

    async def _maybe_synthesize(
        self,
        task_id: str,
        agents: list[str],
        mode: ExecutionMode = ExecutionMode.PARALLEL,
        failures: list[str] | None = None,
    ) -> None:
        """Synthesize outputs from multiple agents into one response, if applicable."""
        if failures:
            return  # Partial data — skip synthesis to avoid a confidently wrong summary.
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
        results = await asyncio.gather(
            *[self._run_agent_with_retry(agent, payload) for agent in agents],
            return_exceptions=True,
        )

        failed: list[str] = []
        for agent, result in zip(agents, results, strict=False):
            if isinstance(result, asyncio.CancelledError):
                # A cancelled task means cancel_task() was called — propagate
                # so the outer _process_task handler can write the ledger entry.
                raise result
            if isinstance(result, Exception):
                error_type = classify_error(result)
                logger.error(
                    "Agent '%s' failed in task '%s' — error_type=%s: %s",
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
                await self._handle_agent_result(task_id, agent, result)
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

        t = asyncio.create_task(_refresh())
        self._background_tasks.add(t)
        t.add_done_callback(self._background_tasks.discard)

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
                # The failed attempt may have streamed partial output — tell
                # the UI to discard it before the retry re-streams the answer.
                await self._stream_manager.emit(task_id, "stream_reset", {"agent": agent.name})
                self._maybe_refresh_pools_background()

    async def _verify_agent_claims(self, task_id: str, agent: Agent, result: AgentResult) -> None:
        """Flag final-answer claims unsupported by tool evidence.

        Agents narrate actions ("created the file", "tests pass") the model has no
        way of knowing are true. This cross-checks such claims against the tools
        that actually succeeded. Non-fatal: it annotates the output and records a
        `claims_unverified` ledger entry so a fabricated completion is visible
        rather than silently recorded as clean.
        """
        # Only agentic agents report tool evidence (successful_tools is a list,
        # possibly empty); questions/approvals carry no completion claims.
        if result.successful_tools is None or result.requires_approval or result.has_question:
            return
        violations = verify_claims(result.output, result.successful_tools)
        if not violations:
            return

        bullets = "\n".join(f"- {v}" for v in violations)
        result.output = (
            f"{result.output}\n\n---\n"
            "⚠️ **Unverified claims** — no tool evidence was found for part of this answer:\n"
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

    async def _handle_agent_result(self, task_id: str, agent: Agent, result: AgentResult) -> None:
        """Write result to task context, ledger, and notify user if needed."""
        await self._verify_agent_claims(task_id, agent, result)
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
            )
        )

        if result.requires_approval or result.has_question:
            card_type = CardType.QUESTION if result.has_question else CardType.APPROVAL
            card = Card(
                id=generate_id(),
                type=card_type,
                task_id=task_id,
                agent=agent.name,
                title=f"{agent.name.capitalize()} Agent — Action Required",
                message=result.question or result.output,
                options=result.question_options if result.has_question else ["Approve", "Reject"],
            )
            await self._notify(card)
        else:
            card = Card(
                id=generate_id(),
                type=CardType.INFORMATION,
                task_id=task_id,
                agent=agent.name,
                title=f"{agent.name.capitalize()} — Done",
                message=result.summary,
            )
            await self._notify(card)
