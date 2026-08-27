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

_MAX_TRACKED_TASKS: int = 2000


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

    def get_inner(self) -> InferenceRouter:
        """Return the wrapped router (e.g. for live reload hooks)."""
        return self._inner

    def pop_task_cost(self, task_id: str) -> float:
        """Return accumulated cost for task_id and remove it from the store."""
        return self._task_costs.pop(task_id, 0.0)

    def _add_cost(self, task_id: str | None, cost_usd: float) -> None:
        if task_id and cost_usd:
            self._task_costs[task_id] = self._task_costs.get(task_id, 0.0) + cost_usd
            if len(self._task_costs) > _MAX_TRACKED_TASKS:
                oldest = next(iter(self._task_costs))
                self._task_costs.pop(oldest, None)

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
