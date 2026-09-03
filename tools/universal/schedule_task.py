"""Tool that lets agents schedule one-shot or recurring tasks."""

from __future__ import annotations

from jobs.models import Job, JobPriority, JobType
from tools.base import Tool
from tools.models import ToolInput, ToolOutput
from tools.universal._schedules import entry_view, resolve_zone_name, schedule_name
from utils.ids import generate_id
from utils.time import format_local, from_epoch, parse_local


class ScheduleTaskTool(Tool):
    name = "schedule_task"
    description = (
        "Schedule a task for north to run later in the background, even when the user is "
        "not chatting. Give the work as a natural-language prompt in 'task'; it runs at the "
        "scheduled time under the named agent. Times are the USER'S LOCAL TIME - pass the "
        "hour they said, do not convert to UTC. For a single future run, pass run_at as "
        "'YYYY-MM-DDTHH:MM' local (an explicit offset or trailing Z is honoured if given). "
        "For a repeating run, pass hour (0-23) plus optional minute (0-59) and weekday "
        "(0=Mon … 6=Sun; omit for daily). Pass tz only to schedule in a zone other than the "
        "user's own, as an IANA name like 'Asia/Kolkata'. Good for reminders, digests, and "
        "check-ins. Use list_schedules to see what is scheduled, update_schedule to change "
        "one, and cancel_schedule to remove one."
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
            "run_at": {"type": "string", "description": "Local ISO 8601 datetime for a one-shot run"},
            "hour": {"type": "integer", "description": "Hour (0-23), local, for a recurring schedule"},
            "minute": {"type": "integer", "description": "Minute (0-59, default 0)"},
            "weekday": {"type": "integer", "description": "Weekday 0=Mon…6=Sun (omit for daily)"},
            "tz": {"type": "string", "description": "IANA zone, only if not the user's own"},
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
            epoch = parse_local(run_at)
        except ValueError as exc:
            return ToolOutput(success=False, error=f"Invalid run_at: {exc}")

        job = Job(
            job_id=generate_id(),
            type=JobType.ASYNC,
            agent=agent,
            task=task,
            payload={"scheduled_by": "schedule_task"},
            priority=JobPriority.MEDIUM,
            scheduled_at=from_epoch(epoch),
        )
        await self._job_processor.enqueue(job)
        return ToolOutput(
            success=True,
            data={
                "type": "one-shot",
                "job_id": job.job_id,
                "runs_at": format_local(epoch),
                "runs_at_epoch": epoch,
                "task": task,
                "agent": agent,
            },
        )

    async def _recurring(self, task: str, agent: str, params: dict) -> ToolOutput:
        from jobs.scheduler import CronEntry

        tz = resolve_zone_name(params.get("tz"))
        try:
            weekday_raw = params.get("weekday")
            entry = CronEntry(
                name=schedule_name(task),
                agent=agent,
                task=task,
                hour=int(params["hour"]),
                minute=int(params.get("minute", 0)),
                weekday=int(weekday_raw) if weekday_raw is not None else None,
                tz=tz,
            )
        except (ValueError, KeyError, TypeError) as exc:
            return ToolOutput(success=False, error=str(exc))

        await self._cron_store.add(
            name=entry.name,
            agent=entry.agent,
            task=entry.task,
            hour=entry.hour,
            minute=entry.minute,
            weekday=entry.weekday,
            tz=entry.tz,
        )
        row = await self._cron_store.get(entry.name)
        return ToolOutput(success=True, data={"type": "recurring", **entry_view(row)})

    def format_output(self, data: dict) -> str:
        if data.get("type") == "one-shot":
            return f"Scheduled once: {data['task']} - runs {data['runs_at']} (job {data['job_id']})."
        return f"Scheduled {data['schedule']}: {data['task']} - next run {data['next_run']}."
