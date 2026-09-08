"""Tool that stops a scheduled task from running again."""

from __future__ import annotations

from tools.base import Tool
from tools.models import ToolInput, ToolOutput
from tools.universal._schedules import BUILTIN_NAMES


class CancelScheduleTool(Tool):
    name = "cancel_schedule"
    excluded_domains = frozenset({"engineering"})
    description = (
        "Stop a scheduled task permanently: pass the 'name' of a recurring schedule, or "
        "the 'job_id' of a one-shot task, as shown by list_schedules. A recurring schedule "
        "is deleted, so it never fires again - to move it to a different time instead, use "
        "update_schedule. A schedule north ships with cannot be deleted; cancelling one that "
        "has been edited restores its shipped settings, and pausing it via update_schedule is "
        "how to stop it. This does not stop a run already in progress; 'north cancel' does."
    )
    is_mutating = True
    parameters_schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Recurring schedule name from list_schedules"},
            "job_id": {"type": "string", "description": "One-shot job id from list_schedules"},
        },
    }

    def __init__(self, job_processor, cron_store) -> None:
        self._job_processor = job_processor
        self._cron_store = cron_store

    async def run(self, input: ToolInput) -> ToolOutput:
        name = str(input.params.get("name", "")).strip()
        job_id = str(input.params.get("job_id", "")).strip()
        if not name and not job_id:
            return ToolOutput(success=False, error="Provide 'name' (recurring) or 'job_id' (one-shot).")

        if name:
            removed = await self._cron_store.remove(name)
            if removed:
                # Removing the stored row for a built-in undoes the edit rather
                # than the schedule: the shipped version comes back.
                kind = "restored" if name in BUILTIN_NAMES else "recurring"
                return ToolOutput(success=True, data={"cancelled": name, "type": kind})
            if name in BUILTIN_NAMES:
                return ToolOutput(
                    success=False,
                    error=(
                        f"{name!r} ships with north, so there is nothing to delete. "
                        f"Pause it with update_schedule (enabled false) to stop it running."
                    ),
                )
            return ToolOutput(
                success=False,
                error=f"No schedule named {name!r}. Call list_schedules to see what exists.",
            )

        job = await self._job_processor.get(job_id)
        if job is None:
            return ToolOutput(success=False, error=f"No job {job_id!r}.")
        await self._job_processor.cancel(job_id)
        return ToolOutput(success=True, data={"cancelled": job_id, "type": "one-shot", "task": job.task})

    def format_output(self, data: dict) -> str:
        if data["type"] == "restored":
            return f"{data['cancelled']} is back to the settings north ships with."
        return f"Cancelled {data['type']} schedule {data['cancelled']} - it will not run again."
