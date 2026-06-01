"""Tool that lets agents schedule one-shot or recurring tasks."""

from __future__ import annotations

import re
from datetime import datetime

from jobs.models import Job, JobPriority, JobType
from tools.base import Tool
from tools.models import ToolInput, ToolOutput
from utils.ids import generate_id

_WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


class ScheduleTaskTool(Tool):
    name = "schedule_task"
    description = (
        "Schedule a task to run in the future. "
        "One-shot: provide run_at (ISO 8601 UTC). "
        "Recurring: provide hour (0-23) and optional minute/weekday."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "The task prompt to run"},
            "agent": {
                "type": "string",
                "description": "Agent to run it (default 'general')",
                "default": "general",
            },
            "run_at": {"type": "string", "description": "ISO 8601 UTC datetime for one-shot"},
            "hour": {"type": "integer", "description": "Hour (0-23) for recurring schedule"},
            "minute": {"type": "integer", "description": "Minute (0-59, default 0)"},
            "weekday": {"type": "integer", "description": "Weekday 0=Mon…6=Sun (omit for daily)"},
        },
        "required": ["task"],
    }

    def __init__(self, job_processor, cron_store) -> None:
        self._job_processor = job_processor
        self._cron_store = cron_store

    async def run(self, input: ToolInput) -> ToolOutput:
        task = str(input.params.get("task", "")).strip()
        if not task:
            return ToolOutput(success=False, error="Parameter 'task' is required.")

        agent = str(input.params.get("agent", "general"))
        run_at = input.params.get("run_at")
        hour = input.params.get("hour")

        if run_at is not None:
            return await self._one_shot(task, agent, str(run_at))
        if hour is not None:
            return await self._recurring(task, agent, input.params)
        return ToolOutput(
            success=False,
            error="Provide 'run_at' for a one-shot task or 'hour' for a recurring schedule.",
        )

    async def _one_shot(self, task: str, agent: str, run_at: str) -> ToolOutput:
        try:
            scheduled_at = datetime.fromisoformat(run_at.replace("Z", "+00:00"))
        except ValueError as exc:
            return ToolOutput(success=False, error=f"Invalid run_at: {exc}")

        job = Job(
            job_id=generate_id(),
            type=JobType.ASYNC,
            agent=agent,
            task=task,
            payload={"scheduled_by": "schedule_task"},
            priority=JobPriority.MEDIUM,
            scheduled_at=scheduled_at,
        )
        await self._job_processor.enqueue(job)
        return ToolOutput(
            success=True,
            data={
                "type": "one-shot",
                "job_id": job.job_id,
                "scheduled_at": scheduled_at.isoformat(),
                "task": task,
                "agent": agent,
            },
        )

    async def _recurring(self, task: str, agent: str, params: dict) -> ToolOutput:
        from jobs.scheduler import CronEntry

        try:
            hour = int(params["hour"])
            minute = int(params.get("minute", 0))
            weekday_raw = params.get("weekday")
            weekday = int(weekday_raw) if weekday_raw is not None else None
            entry = CronEntry(
                name="user_" + re.sub(r"[^a-z0-9]+", "_", task.lower())[:40].strip("_"),
                agent=agent,
                task=task,
                hour=hour,
                minute=minute,
                weekday=weekday,
            )
        except (ValueError, KeyError) as exc:
            return ToolOutput(success=False, error=str(exc))

        await self._cron_store.add(
            name=entry.name,
            agent=entry.agent,
            task=entry.task,
            hour=entry.hour,
            minute=entry.minute,
            weekday=entry.weekday,
        )

        if weekday is None:
            schedule = f"daily at {hour:02d}:{minute:02d} UTC"
        else:
            schedule = f"every {_WEEKDAY_NAMES[weekday]} at {hour:02d}:{minute:02d} UTC"

        return ToolOutput(
            success=True,
            data={
                "type": "recurring",
                "cron_name": entry.name,
                "schedule": schedule,
                "task": task,
                "agent": agent,
            },
        )
