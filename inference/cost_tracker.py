"""CostTracker — decorator that accumulates per-task inference costs.

Wraps any InferenceRouter and intercepts every complete() call to
accumulate cost_usd by task_id. Because all pipeline components
(classifier, north-star checker, router, synthesizer, agents) share the
same wrapped instance, the total reflects every LLM call for a task —
not just agent calls.

Usage:
    tracker = CostTracker(build_router(...))
    # pass `tracker` wherever InferenceRouter is expected
    cost = tracker.pop_task_cost(task_id)   # after task completes

See docs/CODING_STYLE.md Sections 2.2, 3, 6.4.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from inference.base import InferenceRouter
from inference.models import (
    CompletionRequest,
    CompletionResponse,
    EmbedRequest,
    EmbedResponse,
    ModelPool,
    PoolPriority,
    ToolCallRequest,
    ToolCallResponse,
    TranscriptionRequest,
    TranscriptionResponse,
)


class CostTracker(InferenceRouter):
    """InferenceRouter decorator that accumulates cost_usd per task_id.

    complete(), complete_with_tools(), embed(), and transcribe() delegate to
    the wrapped router then add response.cost_usd to the running total for
    request.task_id (if present). pop_task_cost() returns and clears the total
    so the Orchestrator can emit it in task_completed.
    """

    def __init__(self, inner: InferenceRouter) -> None:
        self._inner = inner
        self._task_costs: dict[str, float] = {}

    def pop_task_cost(self, task_id: str) -> float:
        """Return accumulated cost for task_id and remove it from the store."""
        return self._task_costs.pop(task_id, 0.0)

    def _add_cost(self, task_id: str | None, cost_usd: float) -> None:
        if task_id and cost_usd:
            self._task_costs[task_id] = self._task_costs.get(task_id, 0.0) + cost_usd

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        response = await self._inner.complete(request)
        self._add_cost(request.task_id, response.cost_usd)
        return response

    async def complete_with_tools(
        self,
        request: ToolCallRequest,
        token_callback: Callable[[str], Awaitable[None]] | None = None,
    ) -> ToolCallResponse:
        response = await self._inner.complete_with_tools(request, token_callback)
        self._add_cost(request.task_id, response.cost_usd)
        return response

    async def embed(self, request: EmbedRequest) -> EmbedResponse:
        response = await self._inner.embed(request)
        self._add_cost(request.task_id, response.cost_usd)
        return response

    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResponse:
        response = await self._inner.transcribe(request)
        self._add_cost(request.task_id, response.cost_usd)
        return response

    async def get_model(self, priority: PoolPriority) -> str:
        return await self._inner.get_model(priority)

    async def refresh_pools(self) -> None:
        await self._inner.refresh_pools()

    def current_pools(self) -> dict[str, ModelPool]:
        return self._inner.current_pools()

    async def aclose(self) -> None:
        if hasattr(self._inner, "aclose"):
            await self._inner.aclose()
