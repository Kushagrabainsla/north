"""Main Orchestrator — ties Stages 1–4.

See docs/CODING_STYLE.md Sections 2.5, 4.1, 6, 10.2, 14.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from agents import Agent, AgentPayload, AgentResult
from agents.registry import AgentRegistry
from approval import Card, CardType, JudgementFilter, Notifier
from ledger import LedgerEntry, LedgerFilters, LedgerSource, LedgerStatus, LedgerWriter
from inference.cost_tracker import CostTracker
from orchestrator.exceptions import NorthStarConflictError, OrchestratorError, RoutingError
from orchestrator.models import ExecutionMode, ExecutionPlan, IntentClassification, TaskRequest, TaskResponse
from orchestrator.north_star import NorthStarChecker
from orchestrator.router import ExecutionPlanner
from orchestrator.stream import EventStreamManager
from orchestrator.synthesizer import ResultSynthesizer
from orchestrator.task_context import TaskContextStore
from config.strategy import NorthSettings, StrategyMode, describe
from tools.registry import ToolRegistry
from tools.models import ToolInput
from utils.ids import generate_id, generate_task_id
from utils.time import format_timestamp, utcnow

_STRATEGY_RE = re.compile(r"\b(eco|cruise|sport)\b", re.IGNORECASE)


def _format_tool_result(tool_name: str, data: dict | None) -> str:
    """Format a tool result dict as a human-readable string."""
    if not data:
        return "Done."
    if tool_name == "write_file":
        return f"Created `{data.get('path', '?')}` ({data.get('bytes_written', 0)} bytes written)."
    if tool_name == "patch_file":
        return f"Patched `{data.get('path', '?')}`."
    if tool_name == "read_file":
        return str(data.get("content", "(empty)"))
    if tool_name == "list_dir":
        entries = data.get("entries", [])
        return "\n".join(str(e) for e in entries) if entries else "(empty directory)"
    if tool_name == "bash":
        return str(data.get("output", data.get("stdout", ""))).strip()
    if tool_name == "search_files":
        results = data.get("results", [])
        return "\n".join(str(r) for r in results) if results else "No matches found."
    if tool_name == "web_search":
        results = data.get("results", [])
        return "\n".join(str(r) for r in results) if results else "No results."
    return json.dumps(data, indent=2)
_STRATEGY_INTENT_RE = re.compile(
    r"\b(set|switch|use|change|enable|activate|mode)\b", re.IGNORECASE
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
        judgement_filter: JudgementFilter | None = None,
        north_settings: NorthSettings | None = None,
        synthesizer: ResultSynthesizer | None = None,
        cost_tracker: CostTracker | None = None,
        episodic_store: Any | None = None,
        tool_registry: ToolRegistry | None = None,
    ) -> None:
        self._ledger = ledger
        self._agent_registry = agent_registry
        self._north_star_checker = north_star_checker
        self._execution_planner = execution_planner
        self._task_context_store = task_context_store
        self._failure_handler = failure_handler
        self._notifier = notifier
        self._stream_manager = stream_manager
        self._judgement_filter = judgement_filter
        self._north_settings = north_settings
        self._synthesizer = synthesizer
        self._cost_tracker = cost_tracker
        self._episodic_store = episodic_store
        self._tool_registry = tool_registry

    # ------------------------------------------------------------------ #
    #  Public API surface (called by FastAPI routes)                       #
    # ------------------------------------------------------------------ #

    async def submit_task(self, request: TaskRequest) -> TaskResponse:
        """Register and begin processing a new task. Returns immediately."""
        task_id = generate_task_id()
        now = utcnow()

        asyncio.create_task(self._write_ledger(LedgerEntry(
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
        """Write a cancelled entry to the ledger for the given task."""
        asyncio.create_task(self._write_ledger(LedgerEntry(
            id=generate_id(),
            timestamp=utcnow(),
            source=LedgerSource.SYSTEM,
            task_id=task_id,
            action="task_cancelled",
            status=LedgerStatus.CANCELLED,
        )))
        await self._stream_manager.emit(task_id, "task_cancelled", {})

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
        asyncio.create_task(self._write_ledger(LedgerEntry(
            id=generate_id(),
            timestamp=utcnow(),
            source=LedgerSource.APPROVAL,
            task_id=task_id,
            agent=agent,
            action=f"approval_responded: {decision}",
            input=f"card_id={card_id}",
            output=f"chosen_option={chosen_option}",
            status=status,
        )))
        from approval.store import approval_store
        approval_store.resolve(card_id, decision, chosen_option=chosen_option)
        await self._stream_manager.emit(task_id, "approval_responded", {
            "card_id": card_id,
            "decision": decision,
            "chosen_option": chosen_option,
        })

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
        """Run judgement filter; auto-resolve or surface to user."""
        if self._judgement_filter is not None:
            decision, chosen_option = await self._judgement_filter.check(card)
            if decision is not None:
                asyncio.create_task(self._write_ledger(LedgerEntry(
                    id=generate_id(),
                    timestamp=utcnow(),
                    source=LedgerSource.APPROVAL,
                    task_id=card.task_id,
                    agent=card.agent,
                    action=f"judgement_filter_auto_{decision}",
                    input=card.title,
                    output=chosen_option or decision,
                    status=LedgerStatus.COMPLETED,
                )))
                from approval.store import approval_store
                approval_store.resolve(card.id, decision)
                return
        await self._notifier.notify(card)

    # ------------------------------------------------------------------ #
    #  Stage pipeline                                                      #
    # ------------------------------------------------------------------ #

    def _detect_strategy_command(self, prompt: str) -> StrategyMode | None:
        """Return a StrategyMode if the prompt is a strategy change command."""
        match = _STRATEGY_RE.search(prompt)
        if match and _STRATEGY_INTENT_RE.search(prompt):
            return StrategyMode(match.group(1).lower())
        return None

    async def _process_task(self, task_id: str, request: TaskRequest) -> None:
        """Full pipeline: classify → north-star → route → execute."""
        try:
            # Strategy command shortcut — handle before full pipeline
            if self._north_settings is not None:
                mode = self._detect_strategy_command(request.prompt)
                if mode is not None:
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
                    return

            classification, plan = await self._stage_plan(task_id, request.prompt)
            await self._stage_north_star(task_id, request.prompt, classification)
            await self._stage_execute(
                task_id, request.prompt, plan, request.workspace, domain=classification.domain
            )
        except NorthStarConflictError as e:
            asyncio.create_task(self._write_ledger(LedgerEntry(
                id=generate_id(),
                timestamp=utcnow(),
                source=LedgerSource.SYSTEM,
                task_id=task_id,
                action="task_cancelled",
                output=str(e),
                status=LedgerStatus.CANCELLED,
            )))
            await self._stream_manager.emit(task_id, "task_cancelled", {"reason": str(e)})
            await self._stream_manager.emit_done(task_id)
        except Exception as e:
            logger.exception("Unhandled error processing task %s: %s", task_id, e)
            asyncio.create_task(self._write_ledger(LedgerEntry(
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

    async def _stage_plan(
        self, task_id: str, prompt: str
    ) -> tuple[IntentClassification, ExecutionPlan]:
        """Stage 1+3: Classify intent and build execution plan in one LLM call."""
        await self._stream_manager.emit(task_id, "classifying", {"prompt": prompt})

        classification, plan = await self._execution_planner.plan_all(prompt, task_id=task_id)

        asyncio.create_task(self._write_ledger(LedgerEntry(
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
        await self._stream_manager.emit(task_id, "routed", {
            "agents": plan.agents,
            "parallel_groups": plan.parallel_groups,
            "mode": plan.mode.value,
        })
        await self._task_context_store.initialize_task(task_id, plan.agents)
        return classification, plan

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
        try:
            aligned, tension, reasoning = await self._north_star_checker.check_alignment(
                prompt, task_id=task_id
            )
        except OrchestratorError as e:
            logger.warning("North Star check skipped (inference unavailable): %s", e)
            await self._stream_manager.emit(task_id, "north_star_aligned", {"reasoning": "check skipped"})
            return

        check_action = "north_star_check_aligned" if aligned else "north_star_check_conflict"
        asyncio.create_task(self._write_ledger(LedgerEntry(
            id=generate_id(),
            timestamp=utcnow(),
            source=LedgerSource.SYSTEM,
            task_id=task_id,
            action=check_action,
            output=reasoning,
            status=LedgerStatus.COMPLETED,
        )))

        if not aligned:
            from approval.store import approval_store
            card = Card(
                id=generate_id(),
                type=CardType.APPROVAL,
                task_id=task_id,
                agent="orchestrator",
                title="North Star Conflict Detected",
                message=tension or "This task conflicts with one of your active goals. Proceed?",
                options=["Proceed anyway", "Cancel"],
            )
            approval_store.add(card)
            await self._stream_manager.emit(task_id, "north_star_conflict", {"tension": tension})
            await self._stream_manager.emit(task_id, "approval_required", {
                "card_id": card.id,
                "task_id": task_id,
                "agent": "orchestrator",
                "title": card.title,
                "message": card.message,
                "options": card.options,
            })
            current = await approval_store.wait_for_decision(card.id, timeout=300.0)
            chosen_opt = (current.chosen_option if current else "").lower()
            if chosen_opt not in ("proceed anyway", "proceed", "approve", "approved", "yes"):
                raise NorthStarConflictError(tension or "North Star conflict")
            # User chose "Proceed anyway" — continue

        await self._stream_manager.emit(task_id, "north_star_aligned", {"reasoning": reasoning})

    async def _stage_execute(
        self,
        task_id: str,
        prompt: str,
        plan: ExecutionPlan,
        workspace: str = "",
        domain: str = "general",
    ) -> None:
        """Stage 4: Execute agents in dependency order, then optionally synthesize."""
        if not workspace:
            from config.settings import settings
            workspace = settings.north_workspace

        if plan.mode == ExecutionMode.SINGLE_TOOL and plan.direct_tool:
            await self._execute_single_tool(task_id, prompt, plan, workspace)
            return

        await self._stream_manager.emit(task_id, "executing", {"agents": plan.agents})

        all_failures: list[str] = []
        if plan.mode == ExecutionMode.HIERARCHICAL:
            prior_context = ""
            for group in plan.parallel_groups:
                agents = [self._agent_registry.get(name) for name in group]
                effective_prompt = (
                    f"{prompt}\n\n## Results from earlier steps\n{prior_context}"
                    if prior_context else prompt
                )
                failed = await self._execute_agent_group(task_id, effective_prompt, agents, workspace)
                all_failures.extend(failed)
                # Collect this group's outputs to feed into the next group
                all_data = await self._task_context_store.get_all(task_id)
                snippets = [
                    f"[{name}]: {(all_data.get(name) or {}).get('output', '')}"
                    for name in group
                    if (all_data.get(name) or {}).get("output")
                ]
                prior_context = "\n\n".join(snippets)
        else:
            for group in plan.parallel_groups:
                agents = [self._agent_registry.get(name) for name in group]
                failed = await self._execute_agent_group(task_id, prompt, agents, workspace)
                all_failures.extend(failed)

        await self._maybe_synthesize(task_id, plan.agents, plan.mode)

        if all_failures:
            names = ", ".join(f"`{n}`" for n in all_failures)
            note = f"\n\n> **Note:** {len(all_failures)} agent(s) did not complete: {names}. Partial results may be missing."
            await self._stream_manager.emit(task_id, "token", {"text": note})
        asyncio.create_task(
            self._record_episode(task_id, prompt, plan.agents, domain)
        )
        await self._finish_task(task_id)

    async def _execute_single_tool(
        self, task_id: str, prompt: str, plan: ExecutionPlan, workspace: str
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
                _format_tool_result(plan.direct_tool, result.data)  # type: ignore[arg-type]
                if result.success
                else f"Tool error: {result.error}"
            )
        except ToolNotFoundError:
            logger.warning("single_tool fallback: tool %r not found, re-routing to agent", plan.direct_tool)
            fallback = self._execution_planner._build_fallback_plan("general", task_id)
            await self._stream_manager.emit(task_id, "executing", {"agents": fallback.agents})
            for group in fallback.parallel_groups:
                agents = [self._agent_registry.get(name) for name in group]
                await self._execute_agent_group(task_id, prompt, agents, workspace)
            await self._finish_task(task_id)
            return
        except Exception as exc:
            output = f"Tool execution error: {exc}"
            success = False

        await self._stream_manager.emit(task_id, "tool_result", {
            "tool": plan.direct_tool, "success": success
        })

        asyncio.create_task(self._write_ledger(LedgerEntry(
            id=generate_id(),
            timestamp=utcnow(),
            source=LedgerSource.AGENT,
            task_id=task_id,
            agent="tool_executor",
            action="agent_completed",
            output=output,
            status=LedgerStatus.COMPLETED,
        )))

        await self._stream_manager.emit(task_id, "token", {"text": output})
        await self._finish_task(task_id)

    async def _finish_task(self, task_id: str) -> None:
        """Write completion ledger entry and emit done events."""
        task_cost_usd = self._cost_tracker.pop_task_cost(task_id) if self._cost_tracker else 0.0
        asyncio.create_task(self._write_ledger(LedgerEntry(
            id=generate_id(),
            timestamp=utcnow(),
            source=LedgerSource.SYSTEM,
            task_id=task_id,
            action="task_completed",
            status=LedgerStatus.COMPLETED,
        )))
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
        summary = f"Task: {prompt[:120]}\nResult: {combined[:400]}"
        try:
            await self._episodic_store.record(
                task_id=task_id, domain=domain, summary=summary
            )
        except Exception:
            logger.debug("Episodic record failed for task %s", task_id)

    async def _maybe_synthesize(
        self, task_id: str, agents: list[str], mode: ExecutionMode = ExecutionMode.PARALLEL
    ) -> None:
        """Synthesize outputs from multiple agents into one response, if applicable.

        Only runs for parallel and hierarchical modes — single_agent produces one
        coherent output that needs no synthesis.
        """
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
        self, task_id: str, prompt: str, agents: list[Agent], workspace: str = ""
    ) -> list[str]:
        """Run a parallel group of agents concurrently; handle per-agent failures.

        Returns the names of any agents that failed after all retries.
        """
        payload = AgentPayload(task_id=task_id, prompt=prompt, workspace=workspace)
        results = await asyncio.gather(
            *[self._run_agent_with_retry(agent, payload) for agent in agents],
            return_exceptions=True,
        )

        failed: list[str] = []
        for agent, result in zip(agents, results):
            if isinstance(result, Exception):
                logger.error("Agent '%s' failed: %s", agent.name, result)
                failed.append(agent.name)
                asyncio.create_task(self._write_ledger(LedgerEntry(
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

        asyncio.create_task(self._write_ledger(LedgerEntry(
            id=generate_id(),
            timestamp=utcnow(),
            source=LedgerSource.AGENT,
            task_id=task_id,
            agent=agent.name,
            action="agent_completed",
            output=result.output,
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
