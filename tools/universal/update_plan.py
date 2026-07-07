"""UpdatePlanTool - maintain a task-scoped checklist (#9 plan-and-track).

The agent calls this to write and continuously update the plan it is executing:
break the task into ordered steps and flip each to `in_progress` then `done` as it
works. The full current plan is returned on every call (and streamed to the UI) so a
long task stays anchored on what is finished and what remains. Requires manual
registration because it needs the shared PlanStore (see orchestrator/app.py).
"""

from __future__ import annotations

import contextlib
from typing import Any

from orchestrator.plan_store import VALID_STATUSES, PlanStore
from tools.base import Tool
from tools.models import ToolInput, ToolOutput


class UpdatePlanTool(Tool):
    name = "update_plan"
    is_mutating = False
    description = (
        "Write or update your working checklist for the current task. Pass the FULL "
        "list of steps every time (it replaces the previous plan). Decompose the task "
        "into ordered steps, mark exactly one as 'in_progress' while you work it, and "
        "flip finished steps to 'done'. Use this to stay on track across a long task."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "steps": {
                "type": "array",
                "description": "The full ordered list of plan steps (replaces the prior plan).",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "description": "What the step does"},
                        "status": {
                            "type": "string",
                            "enum": sorted(VALID_STATUSES),
                            "description": "pending, in_progress, or done",
                        },
                    },
                    "required": ["content"],
                },
            },
        },
        "required": ["steps"],
    }

    def __init__(self, plan_store: PlanStore, stream_manager: Any | None = None) -> None:
        self._store = plan_store
        self._stream = stream_manager

    def format_output(self, data: dict[str, Any]) -> str:
        done, total = data.get("done", 0), data.get("total", 0)
        plan = data.get("plan", "")
        return f"Plan updated ({done}/{total} done):\n{plan}" if plan else "Plan cleared."

    async def run(self, input: ToolInput) -> ToolOutput:
        task_id = (input.params.get("task_id") or "").strip()
        if not task_id:
            return ToolOutput(success=False, error="No task_id in scope; cannot track a plan.")
        raw_steps = input.params.get("steps")
        if not isinstance(raw_steps, list):
            return ToolOutput(success=False, error="Parameter 'steps' must be a list of steps.")

        self._store.set_plan(task_id, raw_steps)
        done, total = self._store.progress(task_id)
        rendered = self._store.render(task_id)

        if self._stream is not None:
            # streaming is best-effort; never fail the tool on UI errors
            with contextlib.suppress(Exception):
                await self._stream.emit(task_id, "plan_updated", {"plan": rendered, "done": done, "total": total})

        return ToolOutput(success=True, data={"plan": rendered, "done": done, "total": total})
