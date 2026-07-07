"""Task-scoped plan/TODO tracking for agents (#9 plan-and-track).

Gives an agent a live checklist it decomposes a task into and ticks off as it
works - the Claude Code "TodoWrite" pattern - so a long task never loses track of
what is done and what remains. Plans are working memory: process-local, keyed by
task_id, and rebuilt if a task is re-run. The current plan is injected into the
agent's context so it stays anchored across ReAct iterations and compaction.
"""

from __future__ import annotations

from dataclasses import dataclass

VALID_STATUSES: frozenset[str] = frozenset({"pending", "in_progress", "done"})
_STATUS_MARK: dict[str, str] = {"pending": "[ ]", "in_progress": "[~]", "done": "[x]"}


@dataclass(frozen=True)
class PlanStep:
    content: str
    status: str  # one of VALID_STATUSES


class PlanStore:
    """In-memory, task-scoped plans. All access is by task_id."""

    def __init__(self) -> None:
        self._plans: dict[str, list[PlanStep]] = {}

    def set_plan(self, task_id: str, raw_steps: list[dict]) -> list[PlanStep]:
        """Replace the plan for *task_id* from a list of {content, status} dicts."""
        steps: list[PlanStep] = []
        for raw in raw_steps:
            content = str(raw.get("content") or raw.get("step") or "").strip()
            if not content:
                continue
            status = str(raw.get("status", "pending")).strip().lower()
            if status not in VALID_STATUSES:
                status = "pending"
            steps.append(PlanStep(content=content, status=status))
        self._plans[task_id] = steps
        return steps

    def get_plan(self, task_id: str) -> list[PlanStep]:
        return self._plans.get(task_id, [])

    def render(self, task_id: str) -> str:
        """Render the plan as a checklist, or '' when there is none."""
        steps = self._plans.get(task_id)
        if not steps:
            return ""
        return "\n".join(f"{_STATUS_MARK.get(s.status, '[ ]')} {s.content}" for s in steps)

    def progress(self, task_id: str) -> tuple[int, int]:
        """Return (done, total) for *task_id*."""
        steps = self._plans.get(task_id, [])
        return sum(1 for s in steps if s.status == "done"), len(steps)

    def clear(self, task_id: str) -> None:
        self._plans.pop(task_id, None)
