"""Main Orchestrator — ties Stages 1–4.

See docs/CODING_STYLE.md Sections 2.5, 4.1, 6, 10.2, 14.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from agents import Agent, AgentPayload, AgentResult
from agents.registry import AgentRegistry
from approval import Card, CardType, JudgementFilter, Notifier
from approval.store import ApprovalStore
from config.strategy import NorthSettings, StrategyMode, describe
from inference.cost_tracker import CostTracker
from inference.models import CompletionRequest, PoolPriority
from ledger import LedgerEntry, LedgerFilters, LedgerSource, LedgerStatus, LedgerWriter
from orchestrator.exceptions import NorthStarConflictError, OrchestratorError
from orchestrator.failure_handler import FailureHandler
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
from tools.models import ToolInput
from tools.registry import ToolRegistry
from utils.ids import generate_id, generate_task_id
from utils.logging import bind_task_id
from utils.time import format_timestamp, utcnow

# Maximum tasks allowed to be in-flight at the same time.  Prevents runaway
# webhook integrations or buggy clients from burning API credits.
_MAX_CONCURRENT_TASKS = 10

# Classifier confidence below this threshold skips the north star check to
# avoid interrupting the user on borderline-classified tasks.
_NORTH_STAR_CONFIDENCE_THRESHOLD = 0.7

# Matches the exact command form "/strategy <mode>" or bare shorthand like
# "eco mode" / "switch to cruise" so that incidental mentions of these words
# ("I was in sport mode") never accidentally mutate the strategy.
_STRATEGY_CMD_RE = re.compile(
    r"^(?:(?:set|switch|use|change|enable|activate)\s+(?:to\s+)?)?(?:the\s+)?"
    r"(eco|cruise|sport)\s*(?:mode|strategy)?$",
    re.IGNORECASE,
)

logger = logging.getLogger(__name__)


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
        cost_tracker: CostTracker | None = None,
        episodic_store: Any | None = None,
        tool_registry: ToolRegistry | None = None,
        default_workspace: str = "",
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
        self._cost_tracker = cost_tracker
        self._episodic_store = episodic_store
        self._tool_registry = tool_registry
        self._default_workspace = default_workspace
        # Maps task_id → running asyncio.Task so cancel_task() can stop it.
        self._active_tasks: dict[str, asyncio.Task] = {}

    # ------------------------------------------------------------------ #
    #  Public API surface (called by FastAPI routes)                       #
    # ------------------------------------------------------------------ #

    async def submit_task(self, request: TaskRequest) -> TaskResponse:
        """Register and begin processing a new task. Returns immediately.

        Raises OrchestratorError when the concurrent-task cap is reached so
        callers (API routes, webhook handler) can return 429 to the client.
        """
        if len(self._active_tasks) >= _MAX_CONCURRENT_TASKS:
            raise OrchestratorError(
                f"Too many concurrent tasks ({len(self._active_tasks)} active, "
                f"max {_MAX_CONCURRENT_TASKS}). Try again once a task finishes."
            )
        task_id = generate_task_id()
        now = utcnow()

        # Await the initial write so get_task() never returns None for a live task.
        await self._write_ledger(LedgerEntry(
            id=generate_id(),
            timestamp=now,
            source=request.source,
            task_id=task_id,
            input=request.prompt,
            action="task_received",
            status=LedgerStatus.PENDING,
        ))

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
        entries = await self._ledger.query(LedgerFilters(task_id=task_id, limit=1))
        if not entries:
            return None
        entry = entries[0]
        return TaskResponse(
            task_id=task_id,
            status=entry.status.value if entry.status else "unknown",
            created_at=format_timestamp(entry.timestamp),
        )

    async def cancel_task(self, task_id: str) -> None:
        """Cancel a running task: stop its pipeline and write a terminal ledger entry."""
        running = self._active_tasks.pop(task_id, None)
        if running is not None and not running.done():
            running.cancel()
        await self._write_ledger(LedgerEntry(
            id=generate_id(),
            timestamp=utcnow(),
            source=LedgerSource.SYSTEM,
            task_id=task_id,
            action="task_cancelled",
            status=LedgerStatus.CANCELLED,
        ))
        await self._stream_manager.emit(task_id, "task_cancelled", {})
        await self._stream_manager.emit_done(task_id)

    async def respond_approval(
        self,
        card_id: str,
        task_id: str,
        agent: str,
        decision: str,
        chosen_option: str,
    ) -> None:
        """Record a user approval decision from the notification callback or Web UI."""
        status = (
            LedgerStatus.APPROVED if decision == "approved" else LedgerStatus.REJECTED
        )
        await self._write_ledger(LedgerEntry(
            id=generate_id(),
            timestamp=utcnow(),
            source=LedgerSource.APPROVAL,
            task_id=task_id,
            agent=agent,
            action=f"approval_responded: {decision}",
            input=f"card_id={card_id}",
            output=f"chosen_option={chosen_option}",
            status=status,
        ))
        self._approval_store.resolve(card_id, decision, chosen_option=chosen_option)
        await self._stream_manager.emit(task_id, "approval_responded", {
            "card_id": card_id,
            "decision": decision,
            "chosen_option": chosen_option,
        })

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
                entry.task_id, entry.action, exc,
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
                await self._write_ledger(LedgerEntry(
                    id=generate_id(),
                    timestamp=utcnow(),
                    source=LedgerSource.APPROVAL,
                    task_id=card.task_id,
                    agent=card.agent,
                    action=f"judgement_filter_auto_{decision}",
                    input=card.title,
                    output=chosen_option or decision,
                    status=LedgerStatus.COMPLETED,
                ))
                self._approval_store.resolve(card.id, decision)
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
        match = _STRATEGY_CMD_RE.match(prompt.strip())
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
        await self._ledger.write(LedgerEntry(
            id=generate_id(),
            timestamp=utcnow(),
            source=LedgerSource.SYSTEM,
            task_id=task_id,
            action="agent_completed",
            agent="orchestrator",
            output=msg,
            status=LedgerStatus.COMPLETED,
        ))
        await self._stream_manager.emit(task_id, "task_completed", {})
        await self._stream_manager.emit_done(task_id)
        return True

    async def _process_task(self, task_id: str, request: TaskRequest) -> None:
        """Full pipeline: classify → north-star → route → execute."""
        bind_task_id(task_id)  # attach correlation ID to every log line in this context
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
            await self._write_ledger(LedgerEntry(
                id=generate_id(),
                timestamp=utcnow(),
                source=LedgerSource.SYSTEM,
                task_id=task_id,
                action="task_cancelled",
                output=str(e),
                status=LedgerStatus.CANCELLED,
            ))
            await self._stream_manager.emit(task_id, "task_cancelled", {"reason": str(e)})
            await self._stream_manager.emit_done(task_id)
        except Exception as e:
            logger.exception("Unhandled error processing task %s: %s", task_id, e)
            await self._write_ledger(LedgerEntry(
                id=generate_id(),
                timestamp=utcnow(),
                source=LedgerSource.SYSTEM,
                task_id=task_id,
                action="task_failed",
                output=str(e),
                status=LedgerStatus.FAILED,
            ))
            await self._stream_manager.emit(task_id, "task_failed", {"error": str(e)})
            await self._stream_manager.emit_done(task_id)

    async def _stage_plan(
        self, task_id: str, prompt: str
    ) -> tuple[IntentClassification, ExecutionPlan]:
        """Stages 1+3: Classify intent and build execution plan in one LLM call."""
        await self._stream_manager.emit(task_id, "classifying", {"prompt": prompt})

        classification, plan = await self._execution_planner.plan_all(prompt, task_id=task_id)

        await self._write_ledger(LedgerEntry(
            id=generate_id(),
            timestamp=utcnow(),
            source=LedgerSource.SYSTEM,
            task_id=task_id,
            action=f"classified_as_{'consequential' if classification.is_consequential else 'trivial'}",
            output=classification.reasoning,
            status=LedgerStatus.COMPLETED,
        ))

        await self._stream_manager.emit(task_id, "classified", {
            "is_consequential": classification.is_consequential,
            "domain": classification.domain,
            "reasoning": classification.reasoning,
        })
        await self._stream_manager.emit(task_id, "routed", {
            "agents": plan.agents,
            "parallel_groups": plan.parallel_groups,
            "mode": plan.mode.value,
        })
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
        current = await self._approval_store.wait_for_decision(card.id, timeout=300.0)
        if current is None:
            logger.warning(
                "North Star approval timed out for task %s — treating as rejection",
                task_id,
            )
        chosen_opt = (current.chosen_option if current else "").lower()
        if chosen_opt not in ("proceed anyway", "proceed", "approve", "approved", "yes"):
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
        if classification.confidence < _NORTH_STAR_CONFIDENCE_THRESHOLD:
            logger.info(
                "Skipping north star check for task %s — classifier confidence "
                "%.2f < %.2f threshold",
                task_id,
                classification.confidence,
                _NORTH_STAR_CONFIDENCE_THRESHOLD,
            )
            await self._stream_manager.emit(
                task_id,
                "north_star_aligned",
                {"reasoning": "skipped: low-confidence consequential classification"},
            )
            return

        await self._stream_manager.emit(task_id, "north_star_checking", {})
        try:
            aligned, tension, reasoning = await self._north_star_checker.check_alignment(
                prompt, task_id=task_id
            )
        except OrchestratorError as e:
            logger.warning("North Star check skipped (inference unavailable): %s", e)
            await self._stream_manager.emit(task_id, "north_star_aligned", {"reasoning": "check skipped"})
            return

        check_action = "north_star_check_aligned" if aligned else "north_star_check_conflict"
        await self._write_ledger(LedgerEntry(
            id=generate_id(),
            timestamp=utcnow(),
            source=LedgerSource.SYSTEM,
            task_id=task_id,
            action=check_action,
            output=reasoning,
            status=LedgerStatus.COMPLETED,
        ))

        if not aligned:
            await self._handle_alignment_conflict(task_id, tension)

        await self._stream_manager.emit(task_id, "north_star_aligned", {"reasoning": reasoning})

    async def _execute_hierarchical_groups(
        self, task_id: str, prompt: str, plan: ExecutionPlan, workspace: str, context: str = ""
    ) -> list[str]:
        """Execute agents in hierarchical mode, passing results from earlier steps."""
        all_failures: list[str] = []
        prior_context = ""
        for group in plan.parallel_groups:
            agents = [self._agent_registry.get(name) for name in group]
            effective_prompt = (
                f"{prompt}\n\n## Results from earlier steps\n{prior_context}"
                if prior_context else prompt
            )
            failed = await self._execute_agent_group(
                task_id, effective_prompt, agents, workspace, context=context
            )
            all_failures.extend(failed)
            
            all_data = await self._task_context_store.get_all(task_id)
            snippets = [
                f"[{name}]: {(all_data.get(name) or {}).get('output', '')}"
                for name in group
                if (all_data.get(name) or {}).get("output")
            ]
            prior_context = "\n\n".join(snippets)
        return all_failures

    async def _execute_parallel_groups(
        self, task_id: str, prompt: str, plan: ExecutionPlan, workspace: str, context: str = ""
    ) -> list[str]:
        """Execute agents in parallel groups."""
        all_failures: list[str] = []
        for group in plan.parallel_groups:
            agents = [self._agent_registry.get(name) for name in group]
            failed = await self._execute_agent_group(
                task_id, prompt, agents, workspace, context=context
            )
            all_failures.extend(failed)
        return all_failures

    async def _report_execution_failures(self, task_id: str, failures: list[str]) -> None:
        """Format and emit a message showing which agents failed to complete."""
        names = ", ".join(f"`{n}`" for n in failures)
        note = (
            f"\n\n> **Note:** {len(failures)} agent(s) did not complete: {names}. "
            "Partial results may be missing."
        )
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
            all_failures = await self._execute_hierarchical_groups(
                task_id, prompt, plan, workspace, context=context
            )
        else:
            all_failures = await self._execute_parallel_groups(
                task_id, prompt, plan, workspace, context=context
            )

        await self._maybe_synthesize(task_id, plan.agents, plan.mode)

        if all_failures:
            await self._report_execution_failures(task_id, all_failures)

        asyncio.create_task(
            self._record_episode(task_id, prompt, plan.agents, domain)
        )
        await self._finish_task(task_id)

    async def _execute_single_tool(
        self, task_id: str, prompt: str, plan: ExecutionPlan, workspace: str, context: str = ""
    ) -> None:
        """Execute a single tool call directly, bypassing the agent layer."""
        from tools.exceptions import ToolNotFoundError

        await self._stream_manager.emit(task_id, "executing", {"agents": []})
        await self._stream_manager.emit(task_id, "tool_called", {
            "tool": plan.direct_tool, "params": plan.direct_tool_params
        })

        output = ""
        success = False
        try:
            if self._tool_registry is None:
                raise ToolNotFoundError("No tool registry available.")
            tool = self._tool_registry.get(plan.direct_tool)  # type: ignore[arg-type]
            params = {**plan.direct_tool_params}
            if workspace and "workspace" not in params:
                params["workspace"] = workspace
            result = await tool.run(ToolInput(params=params))
            success = result.success
            output = (
                tool.format_output(result.data)
                if result.success
                else f"Tool error: {result.error}"
            )
        except ToolNotFoundError:
            logger.warning("single_tool fallback: tool %r not found, re-routing to agent", plan.direct_tool)
            fallback = self._execution_planner._build_fallback_plan("general", task_id)
            await self._stream_manager.emit(task_id, "executing", {"agents": fallback.agents})
            fallback_failures: list[str] = []
            for group in fallback.parallel_groups:
                agents = [self._agent_registry.get(name) for name in group]
                failed = await self._execute_agent_group(
                    task_id, prompt, agents, workspace, context=context
                )
                fallback_failures.extend(failed)
            if fallback_failures:
                await self._report_execution_failures(task_id, fallback_failures)
            await self._finish_task(task_id)
            return
        except Exception as exc:
            output = f"Tool execution error: {exc}"
            success = False

        await self._stream_manager.emit(task_id, "tool_result", {
            "tool": plan.direct_tool, "success": success
        })

        await self._write_ledger(LedgerEntry(
            id=generate_id(),
            timestamp=utcnow(),
            source=LedgerSource.AGENT,
            task_id=task_id,
            agent="tool_executor",
            action="agent_completed",
            output=output,
            status=LedgerStatus.COMPLETED,
        ))

        await self._stream_manager.emit(task_id, "token", {"text": output})
        await self._finish_task(task_id)

    async def _finish_task(self, task_id: str) -> None:
        """Write completion ledger entry and emit done events."""
        task_cost_usd = self._cost_tracker.pop_task_cost(task_id) if self._cost_tracker else 0.0
        await self._write_ledger(LedgerEntry(
            id=generate_id(),
            timestamp=utcnow(),
            source=LedgerSource.SYSTEM,
            task_id=task_id,
            action="task_completed",
            status=LedgerStatus.COMPLETED,
        ))
        await self._stream_manager.emit(task_id, "task_completed", {"cost_usd": task_cost_usd})
        await self._stream_manager.emit_done(task_id)

    async def _record_episode(
        self, task_id: str, prompt: str, agents: list[str], domain: str
    ) -> None:
        """Write a task episode to episodic memory after completion."""
        if self._episodic_store is None:
            return
        all_data = await self._task_context_store.get_all(task_id)
        outputs = [
            (all_data.get(agent) or {}).get("output", "") for agent in agents
        ]
        combined = "\n".join(o for o in outputs if o).strip()
        if not combined:
            return
        summary = await self._summarize_episode(task_id, prompt, combined)
        try:
            await self._episodic_store.record(
                task_id=task_id, domain=domain, summary=summary
            )
        except Exception:
            logger.debug("Episodic record failed for task %s", task_id)

    async def _summarize_episode(
        self, task_id: str, prompt: str, output: str
    ) -> str:
        """Generate a retrieval-friendly episode summary via the LLM.

        Falls back to a plain truncated string when the cost tracker is
        unavailable (tests, offline mode) so episodic memory always gets
        *something* rather than nothing.
        """
        fallback = f"Task: {prompt[:200]}\nResult: {output[:500]}"
        if self._cost_tracker is None:
            return fallback
        try:
            response = await self._cost_tracker.complete(
                CompletionRequest(
                    prompt=(
                        "Summarize this completed AI task in 2–3 sentences for future retrieval. "
                        "Include what was requested, what was done, and any key outcomes or decisions.\n\n"
                        f"Task: {prompt}\n\nResult: {output[:3000]}"
                    ),
                    priority=PoolPriority.LOW,
                    component="episodic_summary",
                    task_id=task_id,
                )
            )
            return response.text.strip() or fallback
        except Exception:
            return fallback

    async def _maybe_synthesize(
        self, task_id: str, agents: list[str], mode: ExecutionMode = ExecutionMode.PARALLEL
    ) -> None:
        """Synthesize outputs from multiple agents into one response, if applicable."""
        if self._synthesizer is None or len(agents) < 2:
            return
        if mode not in (ExecutionMode.PARALLEL, ExecutionMode.HIERARCHICAL):
            return

        all_data = await self._task_context_store.get_all(task_id)
        agent_outputs = {
            agent: (all_data.get(agent) or {}).get("output", "")
            for agent in agents
        }

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
        for agent, result in zip(agents, results):
            if isinstance(result, asyncio.CancelledError):
                # A cancelled task means cancel_task() was called — propagate
                # so the outer _process_task handler can write the ledger entry.
                raise result
            if isinstance(result, Exception):
                logger.error("Agent '%s' failed: %s", agent.name, result)
                failed.append(agent.name)
                await self._write_ledger(LedgerEntry(
                    id=generate_id(),
                    timestamp=utcnow(),
                    source=LedgerSource.AGENT,
                    task_id=task_id,
                    agent=agent.name,
                    action="agent_execution_failed",
                    output=str(result),
                    status=LedgerStatus.FAILED,
                ))
            else:
                await self._handle_agent_result(task_id, agent, result)
        return failed

    async def _run_agent_with_retry(
        self, agent: Agent, payload: AgentPayload
    ) -> AgentResult:
        """Run an agent, retrying on failure up to the handler's max_retries."""
        task_id = payload.task_id
        await self._stream_manager.emit(task_id, "agent_started", {"agent": agent.name})

        while True:
            try:
                result = await agent.run(payload)
                self._failure_handler.clear_retry_count(task_id, agent.name)
                await self._task_context_store.update_agent_status(
                    task_id, agent.name, "completed"
                )
                await self._stream_manager.emit(
                    task_id, "agent_completed",
                    {"agent": agent.name, "summary": result.summary},
                )
                return result
            except Exception as exc:
                should_retry = await self._failure_handler.handle_failure(
                    task_id, agent.name, exc
                )
                if not should_retry:
                    raise

    async def _handle_agent_result(
        self, task_id: str, agent: Agent, result: AgentResult
    ) -> None:
        """Write result to task context, ledger, and notify user if needed."""
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

        await self._write_ledger(LedgerEntry(
            id=generate_id(),
            timestamp=utcnow(),
            source=LedgerSource.AGENT,
            task_id=task_id,
            agent=agent.name,
            action="agent_completed",
            output=result.output,
            agent_output=result.data,
            status=LedgerStatus.COMPLETED,
        ))

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
