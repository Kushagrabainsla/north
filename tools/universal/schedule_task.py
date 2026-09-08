"""Tool that lets agents schedule one-shot or recurring tasks."""

from __future__ import annotations

from jobs.models import Job, JobPriority, JobType
from tools.base import Tool
from tools.models import ToolInput, ToolOutput
from tools.universal._schedules import entry_view, parse_weekdays, resolve_zone_name
from utils.ids import generate_id
from utils.time import format_local, from_epoch, parse_local


def _whole_number(params: dict, field: str, ceiling: int, default: int | None = None) -> int:
    """One numeric field, read the way a person would check it.

    Raises ValueError naming the field and its range. Reporting these as a bare
    ``int()`` TypeError told the caller nothing about what it should send
    instead, so a recoverable mistake ended the task.
    """
    raw = params.get(field)
    if raw is None:
        if default is None:
            raise ValueError(f"{field} is required for a repeating schedule (0-{ceiling})")
        return default
    if isinstance(raw, bool | list | tuple | dict):
        raise ValueError(f"{field} must be a single number in 0-{ceiling}, got {raw!r}")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be a number in 0-{ceiling}, got {raw!r}") from None
    if not 0 <= value <= ceiling:
        raise ValueError(f"{field} must be in 0-{ceiling}, got {value}")
    return value


class ScheduleTaskTool(Tool):
    name = "schedule_task"
    excluded_domains = frozenset({"engineering"})
    description = (
        "Schedule a task for north to run later in the background, even when the user is "
        "not chatting. Give the work as a natural-language prompt in 'task'; it runs at the "
        "scheduled time under the named agent. Times are the USER'S LOCAL TIME - pass the "
        "hour they said, do not convert to UTC. For a single future run, pass run_at as "
        "'YYYY-MM-DDTHH:MM' local (an explicit offset or trailing Z is honoured if given). "
        "For a repeating run, pass hour (0-23) plus optional minute (0-59) and days (omit "
        "days for every day). 'days' takes a list of day names or numbers (0=Mon … 6=Sun), "
        "or one of the words 'weekdays', 'weekends', 'daily' - so \"every weekday at 9:30\" "
        "is hour 9, minute 30, days 'weekdays'. Pass tz only to schedule in a zone other "
        "than the user's own, as an IANA name like 'Asia/Kolkata'. Good for reminders, "
        "digests, and check-ins. Use list_schedules to see what is scheduled, "
        "update_schedule to change one, and cancel_schedule to remove one."
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
            "days": {
                "description": (
                    "Which days it runs: a list of day names or numbers (0=Mon…6=Sun), or "
                    "'weekdays' / 'weekends' / 'daily'. Omit for every day."
                ),
            },
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
        # `weekday` is the old single-day spelling; still read so a caller working
        # from a cached description of this tool is not simply refused.
        days = params.get("days", params.get("weekday"))
        try:
            entry = CronEntry(
                name=await self._cron_store.unique_name(task),
                agent=agent,
                task=task,
                hour=_whole_number(params, "hour", 23),
                minute=_whole_number(params, "minute", 59, default=0),
                weekdays=parse_weekdays(days),
                tz=tz,
            )
        except ValueError as exc:
            return ToolOutput(success=False, error=str(exc))

        await self._cron_store.add(
            name=entry.name,
            agent=entry.agent,
            task=entry.task,
            hour=entry.hour,
            minute=entry.minute,
            weekdays=entry.weekdays,
            tz=entry.tz,
        )
        row = await self._cron_store.get(entry.name)
        return ToolOutput(success=True, data={"type": "recurring", **entry_view(row)})

    def format_output(self, data: dict) -> str:
        if data.get("type") == "one-shot":
            return f"Scheduled once: {data['task']} - runs {data['runs_at']} (job {data['job_id']})."
        return f"Scheduled {data['schedule']}: {data['task']} - next run {data['next_run']}."
