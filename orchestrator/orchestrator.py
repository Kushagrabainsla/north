"""Main Orchestrator — ties Stages 1–4.

See docs/CODING_STYLE.md Sections 2.5, 4.1, 6, 10.2, 14.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from agents import Agent, AgentPayload, AgentResult
from agents.registry import AgentRegistry
from approval import Card, CardType, Notifier
from ledger import LedgerEntry, LedgerFilters, LedgerSource, LedgerStatus, LedgerWriter
from orchestrator.classifier import IntentClassifier
from orchestrator.exceptions import NorthStarConflictError, OrchestratorError, RoutingError
from orchestrator.failure_handler import FailureHandler
from orchestrator.models import ExecutionPlan, IntentClassification, TaskRequest, TaskResponse
from orchestrator.north_star import NorthStarChecker
from orchestrator.router import ExecutionPlanner
from orchestrator.stream import EventStreamManager
from orchestrator.task_context import TaskContextStore
from utils.ids import generate_id, generate_task_id
from utils.time import format_timestamp, utcnow

logger = logging.getLogger(__name__)


class Orchestrator:
    """Coordinates the full task lifecycle across all four stages.

    Injected via ``config/dependencies.py``; never instantiated inline.
    """

    def __init__(
        self,
        ledger: LedgerWriter,
        agent_registry: AgentRegistry,
        classifier: IntentClassifier,
        north_star_checker: NorthStarChecker,
        execution_planner: ExecutionPlanner,
        task_context_store: TaskContextStore,
        failure_handler: FailureHandler,
        notifier: Notifier,
        stream_manager: EventStreamManager,
    ) -> None:
        self._ledger = ledger
        self._agent_registry = agent_registry
        self._classifier = classifier
        self._north_star_checker = north_star_checker
        self._execution_planner = execution_planner
        self._task_context_store = task_context_store
        self._failure_handler = failure_handler
        self._notifier = notifier
        self._stream_manager = stream_manager

    # ------------------------------------------------------------------ #
    #  Public API surface (called by FastAPI routes)                       #
    # ------------------------------------------------------------------ #

    async def submit_task(self, request: TaskRequest) -> TaskResponse:
        """Register and begin processing a new task. Returns immediately."""
        task_id = generate_task_id()
        now = utcnow()

        asyncio.create_task(self._ledger.write(LedgerEntry(
            id=generate_id(),
            timestamp=now,
            source=request.source,
            task_id=task_id,
            input=request.prompt,
            action="task_received",
            status=LedgerStatus.PENDING,
        )))

        # Kick off async processing; caller gets the task_id immediately.
        asyncio.create_task(self._process_task(task_id, request))

        return TaskResponse(
            task_id=task_id,
            status=LedgerStatus.PENDING.value,
            created_at=format_timestamp(now),
        )

    async def list_active_tasks(self) -> list[TaskResponse]:
        """Returns tasks that are still pending in the ledger."""
        entries = await self._ledger.query(
            LedgerFilters(source=LedgerSource.PROMPT, limit=50)
        )
        return [
            TaskResponse(
                task_id=e.task_id or "",
                status=(e.status.value if e.status else "unknown"),
                created_at=format_timestamp(e.timestamp),
            )
            for e in entries
            if e.status == LedgerStatus.PENDING
        ]

    # ------------------------------------------------------------------ #
    #  Stage pipeline                                                      #
    # ------------------------------------------------------------------ #

    async def _process_task(self, task_id: str, request: TaskRequest) -> None:
        """Full pipeline: classify → north-star → route → execute."""
        try:
            classification = await self._stage_classify(task_id, request.prompt)
            await self._stage_north_star(task_id, request.prompt, classification)
            plan = await self._stage_route(task_id, request.prompt, classification)
            await self._stage_execute(task_id, request.prompt, plan)
        except NorthStarConflictError:
            # Conflict is already surfaced to the user; swallow here.
            pass
        except Exception as e:
            logger.exception("Unhandled error processing task %s: %s", task_id, e)
            asyncio.create_task(self._ledger.write(LedgerEntry(
                id=generate_id(),
                timestamp=utcnow(),
                source=LedgerSource.SYSTEM,
                task_id=task_id,
                action="task_failed",
                output=str(e),
                status=LedgerStatus.FAILED,
            )))
            await self._stream_manager.emit(task_id, "task_failed", {"error": str(e)})
            await self._stream_manager.emit_done(task_id)

    async def _stage_classify(
        self, task_id: str, prompt: str
    ) -> IntentClassification:
        """Stage 1: Classify the prompt."""
        await self._stream_manager.emit(task_id, "classifying", {"prompt": prompt})
        classification = await self._classifier.classify(prompt, task_id=task_id)

        asyncio.create_task(self._ledger.write(LedgerEntry(
            id=generate_id(),
            timestamp=utcnow(),
            source=LedgerSource.SYSTEM,
            task_id=task_id,
            action=f"classified_as_{'consequential' if classification.is_consequential else 'trivial'}",
            output=classification.reasoning,
            status=LedgerStatus.COMPLETED,
        )))

        await self._stream_manager.emit(task_id, "classified", {
            "is_consequential": classification.is_consequential,
            "domain": classification.domain,
            "reasoning": classification.reasoning,
        })
        return classification

    async def _stage_north_star(
        self,
        task_id: str,
        prompt: str,
        classification: IntentClassification,
    ) -> None:
        """Stage 2: North Star alignment check (consequential tasks only)."""
        if not classification.is_consequential:
            return

        await self._stream_manager.emit(task_id, "north_star_checking", {})
        aligned, tension, reasoning = await self._north_star_checker.check_alignment(
            prompt, task_id=task_id
        )

        check_action = "north_star_check_aligned" if aligned else "north_star_check_conflict"
        asyncio.create_task(self._ledger.write(LedgerEntry(
            id=generate_id(),
            timestamp=utcnow(),
            source=LedgerSource.SYSTEM,
            task_id=task_id,
            action=check_action,
            output=reasoning,
            status=LedgerStatus.COMPLETED,
        )))

        if not aligned:
            await self._stream_manager.emit(task_id, "north_star_conflict", {"tension": tension})
            card = Card(
                id=generate_id(),
                type=CardType.APPROVAL,
                task_id=task_id,
                agent="orchestrator",
                title="North Star Conflict Detected",
                message=tension or "This task conflicts with one of your active goals. Proceed?",
                options=["Proceed anyway", "Cancel"],
            )
            await self._notifier.notify(card)
            raise NorthStarConflictError(tension or "North Star conflict")

        await self._stream_manager.emit(task_id, "north_star_aligned", {"reasoning": reasoning})

    async def _stage_route(
        self,
        task_id: str,
        prompt: str,
        classification: IntentClassification,
    ) -> ExecutionPlan:
        """Stage 3: Build the execution plan."""
        await self._stream_manager.emit(task_id, "routing", {"domain": classification.domain})
        plan = await self._execution_planner.plan(prompt, classification, task_id)

        asyncio.create_task(self._ledger.write(LedgerEntry(
            id=generate_id(),
            timestamp=utcnow(),
            source=LedgerSource.SYSTEM,
            task_id=task_id,
            action="routed",
            output=f"agents={plan.agents}",
            status=LedgerStatus.COMPLETED,
        )))

        await self._stream_manager.emit(task_id, "routed", {
            "agents": plan.agents,
            "parallel_groups": plan.parallel_groups,
        })
        await self._task_context_store.initialize_task(task_id, plan.agents)
        return plan

    async def _stage_execute(
        self, task_id: str, prompt: str, plan: ExecutionPlan
    ) -> None:
        """Stage 4: Execute agents in dependency order, in parallel groups."""
        await self._stream_manager.emit(task_id, "executing", {"agents": plan.agents})

        for group in plan.parallel_groups:
            agents = [self._agent_registry.get(name) for name in group]
            await self._execute_agent_group(task_id, prompt, agents)

        # Mark task completed
        asyncio.create_task(self._ledger.write(LedgerEntry(
            id=generate_id(),
            timestamp=utcnow(),
            source=LedgerSource.SYSTEM,
            task_id=task_id,
            action="task_completed",
            status=LedgerStatus.COMPLETED,
        )))
        await self._stream_manager.emit(task_id, "task_completed", {})
        await self._stream_manager.emit_done(task_id)

    async def _execute_agent_group(
        self, task_id: str, prompt: str, agents: list[Agent]
    ) -> None:
        """Run a parallel group of agents concurrently; handle per-agent failures."""
        payload = AgentPayload(task_id=task_id, prompt=prompt)
        results = await asyncio.gather(
            *[self._run_agent_with_retry(agent, payload) for agent in agents],
            return_exceptions=True,
        )

        for agent, result in zip(agents, results):
            if isinstance(result, Exception):
                logger.error("Agent '%s' failed: %s", agent.name, result)
                asyncio.create_task(self._ledger.write(LedgerEntry(
                    id=generate_id(),
                    timestamp=utcnow(),
                    source=LedgerSource.AGENT,
                    task_id=task_id,
                    agent=agent.name,
                    action="agent_execution_failed",
                    output=str(result),
                    status=LedgerStatus.FAILED,
                )))
            else:
                await self._handle_agent_result(task_id, agent, result)

    async def _run_agent_with_retry(
        self, agent: Agent, payload: AgentPayload
    ) -> AgentResult:
        """Run an agent, retrying on failure up to the handler's max_retries."""
        task_id = payload.task_id
        await self._stream_manager.emit(task_id, "agent_started", {"agent": agent.name})

        while True:
            try:
                result = await agent.run(payload)
                await self._task_context_store.update_agent_status(
                    task_id, agent.name, "completed"
                )
                await self._stream_manager.emit(
                    task_id, "agent_completed", {"agent": agent.name}
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

        asyncio.create_task(self._ledger.write(LedgerEntry(
            id=generate_id(),
            timestamp=utcnow(),
            source=LedgerSource.AGENT,
            task_id=task_id,
            agent=agent.name,
            action="agent_completed",
            output=result.summary,
            agent_output=result.data,
            status=LedgerStatus.COMPLETED,
        )))

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
            await self._notifier.notify(card)
        else:
            card = Card(
                id=generate_id(),
                type=CardType.INFORMATION,
                task_id=task_id,
                agent=agent.name,
                title=f"{agent.name.capitalize()} — Done",
                message=result.summary,
            )
            await self._notifier.notify(card)
