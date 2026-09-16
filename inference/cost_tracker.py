"""CostTracker - decorator that accumulates per-task inference costs.

Wraps any InferenceRouter and intercepts every complete() call to
accumulate cost_usd by task_id. Because all pipeline components
(classifier, north-star checker, router, synthesizer, agents) share the
same wrapped instance, the total reflects every LLM call for a task  -
not just agent calls.

Usage:
    tracker = CostTracker(build_router(...))
    # pass `tracker` wherever InferenceRouter is expected
    cost = tracker.pop_task_cost(task_id)   # after task completes

See docs/CODING_STYLE.md Sections 2.2, 3, 6.4.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any

from inference.base import InferenceRouter
from inference.models import (
    CompletionRequest,
    CompletionResponse,
    EmbedRequest,
    EmbedResponse,
    ModelPool,
    ToolCallRequest,
    ToolCallResponse,
    TranscriptionRequest,
    TranscriptionResponse,
)

_MAX_TRACKED_TASKS: int = 2000
logger = logging.getLogger(__name__)

InferenceCallSink = Callable[[str | None, dict[str, Any]], Awaitable[None]]

_PLANNING_COMPONENTS = frozenset({"planner", "router", "north_star_checker"})
_REVIEW_COMPONENTS = frozenset({"critic", "spec_critique", "judgement_filter"})
_SYNTHESIS_COMPONENTS = frozenset({"synthesizer"})
_MEMORY_COMPONENTS = frozenset(
    {"embed", "extraction_pipeline", "episode_consolidator", "context_injector", "fact_supersede", "fact_glossary"}
)


def inference_call_category(component: str, request_kind: str) -> str:
    """Map detailed components to a small, stable cockpit category."""
    root = component.split(":", 1)[0]
    if component.endswith((":compact", ":tool-summary")):
        return "context"
    if request_kind == "tool_completion":
        return "agent"
    if root in _PLANNING_COMPONENTS:
        return "planning"
    if root in _REVIEW_COMPONENTS:
        return "review"
    if root in _SYNTHESIS_COMPONENTS:
        return "synthesis"
    if root in _MEMORY_COMPONENTS or root.startswith("skill_"):
        return "memory"
    if request_kind == "embedding":
        return "memory"
    if request_kind == "transcription":
        return "perception"
    return "background"


class CostTracker(InferenceRouter):
    """InferenceRouter decorator that accumulates cost_usd per task_id.

    complete(), complete_with_tools(), embed(), and transcribe() delegate to
    the wrapped router then add response.cost_usd to the running total for
    request.task_id (if present). pop_task_cost() returns and clears the total
    so the Orchestrator can emit it in task_completed.
    """

    def __init__(self, inner: InferenceRouter, call_sink: InferenceCallSink | None = None) -> None:
        self._inner = inner
        self._task_costs: OrderedDict[str, float] = OrderedDict()
        self._call_sink = call_sink

    def set_call_sink(self, sink: InferenceCallSink | None) -> None:
        """Attach durable call telemetry after the ledger/stream have been built."""
        self._call_sink = sink

    def get_inner(self) -> InferenceRouter:
        """Return the wrapped router (e.g. for live reload hooks)."""
        return self._inner

    def pop_task_cost(self, task_id: str) -> float:
        """Return accumulated cost for task_id and remove it from the store."""
        return self._task_costs.pop(task_id, 0.0)

    def _add_cost(self, task_id: str | None, cost_usd: float) -> None:
        if task_id and cost_usd:
            self._task_costs[task_id] = self._task_costs.get(task_id, 0.0) + cost_usd
            # Least-recently-charged eviction. Evicting by insertion order would
            # discard a long-running task's accumulated cost while it is still
            # spending, since it was registered before the newer tasks.
            self._task_costs.move_to_end(task_id)
            while len(self._task_costs) > _MAX_TRACKED_TASKS:
                self._task_costs.popitem(last=False)

    async def _record_call(self, request: Any, response: Any, request_kind: str) -> None:
        if self._call_sink is None:
            return
        component = str(getattr(request, "component", "unknown") or "unknown")
        data = {
            "category": inference_call_category(component, request_kind),
            "component": component,
            "request_kind": request_kind,
            "model": str(getattr(response, "model_used", "") or ""),
            "tokens_in": int(getattr(response, "tokens_in", 0) or 0),
            "tokens_out": int(getattr(response, "tokens_out", 0) or 0),
            "cached_tokens": int(getattr(response, "cached_tokens", 0) or 0),
            "cost_usd": float(getattr(response, "cost_usd", 0.0) or 0.0),
            "run_id": getattr(request, "run_id", None),
        }
        try:
            await self._call_sink(getattr(request, "task_id", None), data)
        except Exception:
            logger.warning("Could not record inference call telemetry for %s", component, exc_info=True)

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        response = await self._inner.complete(request)
        self._add_cost(request.task_id, response.cost_usd)
        await self._record_call(request, response, "completion")
        return response

    async def complete_with_tools(
        self,
        request: ToolCallRequest,
        token_callback: Callable[[str], Awaitable[None]] | None = None,
    ) -> ToolCallResponse:
        response = await self._inner.complete_with_tools(request, token_callback)
        self._add_cost(request.task_id, response.cost_usd)
        await self._record_call(request, response, "tool_completion")
        return response

    async def embed(self, request: EmbedRequest) -> EmbedResponse:
        response = await self._inner.embed(request)
        self._add_cost(request.task_id, response.cost_usd)
        await self._record_call(request, response, "embedding")
        return response

    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResponse:
        response = await self._inner.transcribe(request)
        self._add_cost(request.task_id, response.cost_usd)
        await self._record_call(request, response, "transcription")
        return response

    async def refresh_pools(self) -> None:
        await self._inner.refresh_pools()

    def current_pools(self) -> dict[str, ModelPool]:
        return self._inner.current_pools()

    def part_chains(self, limit: int = 6) -> list[dict]:
        return self._inner.part_chains(limit)

    def get_context_window(self, model_id: str) -> int:
        return self._inner.get_context_window(model_id)

    async def aclose(self) -> None:

        if hasattr(self._inner, "aclose"):
            await self._inner.aclose()

    def set_inner(self, inner: InferenceRouter) -> None:
        """Swap the wrapped router in place (runtime config reload).

        Used when a north_config set changes inference keys: the new
        ModelDispatcher is built and assigned here so every component holding
        the CostTracker reference sees the update with no restart.
        """
        self._inner = inner
