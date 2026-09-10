"""Shared task-plan contract for producers and consumers across layers."""

from __future__ import annotations

from typing import Protocol

VALID_PLAN_STATUSES: frozenset[str] = frozenset({"pending", "in_progress", "done"})


class PlanStorePort(Protocol):
    """The narrow plan operations required by tools and agents."""

    def set_plan(self, task_id: str, raw_steps: list[dict]) -> object: ...

    def progress(self, task_id: str) -> tuple[int, int]: ...

    def render(self, task_id: str) -> str: ...
