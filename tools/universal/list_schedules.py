"""Tool that shows everything north is scheduled to do."""

from __future__ import annotations

from tools.base import Tool
from tools.models import ToolInput, ToolOutput
from tools.universal._schedules import builtin_views, entry_view, is_pending_one_shot, job_view
from utils.time import local_timezone_name


class ListSchedulesTool(Tool):
    name = "list_schedules"
    description = (
        "List everything north is scheduled to run: recurring schedules, and one-shot "
        "tasks still waiting to run. Each result carries the name to address it by (or "
        "job_id for a one-shot), what it does, its cadence, and its next run in the "
        "user's local time. A schedule whose source is 'builtin' ships with north and "
        "cannot be changed or cancelled; everything else is the user's own. Call this "
        "before update_schedule or cancel_schedule so you change the one they meant."
    )
    parameters_schema = {"type": "object", "properties": {}}

    def __init__(self, job_processor, cron_store) -> None:
        self._job_processor = job_processor
        self._cron_store = cron_store

    async def run(self, input: ToolInput) -> ToolOutput:
        recurring = [entry_view(row) for row in await self._cron_store.list()] + builtin_views()
        recurring.sort(key=lambda view: view["next_run_epoch"])
        jobs = await self._job_processor.list_jobs(limit=200)
        one_shot = sorted(
            (job_view(job) for job in jobs if is_pending_one_shot(job)),
            key=lambda view: view["next_run_epoch"],
        )
        return ToolOutput(
            success=True,
            data={
                "recurring": recurring,
                "one_shot": one_shot,
                "timezone": local_timezone_name(),
            },
        )

    def format_output(self, data: dict) -> str:
        recurring, one_shot = data.get("recurring", []), data.get("one_shot", [])
        if not recurring and not one_shot:
            return "Nothing is scheduled."
        lines = [f"Schedules (times in {data['timezone']}):"]
        lines += [
            f"  {v['name']}  {v['schedule']}  next {v['next_run']}  - {v['task']}"
            + (" [built-in]" if v["source"] == "builtin" else "")
            for v in recurring
        ]
        lines += [f"  {v['job_id']}  once  {v['next_run']}  - {v['task']}" for v in one_shot]
        return "\n".join(lines)
