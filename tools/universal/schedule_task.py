"""Tool that lets agents schedule one-shot or recurring tasks."""

from __future__ import annotations

from typing import TYPE_CHECKING

from jobs.models import Job, JobPriority, JobType
from jobs.scheduler import CronEntry
from tools.base import Tool
from tools.models import ToolInput, ToolOutput
from tools.universal._schedules import entry_view, parse_weekdays, resolve_zone_name
from utils.ids import generate_id
from utils.time import format_local, from_epoch, now_epoch, parse_local

if TYPE_CHECKING:
    from agents.registry import AgentRegistry
    from flows.registry import FlowRegistry
    from skills.registry import SkillRegistry


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
    description = (
        "Schedule a task for north to run later in the background, even when the user is "
        "not chatting. Give the work as a natural-language prompt in 'task', and a short "
        "title in 'label' so it reads well in a list; the prompt runs at the "
        "scheduled time under the named agent. Times are the USER'S LOCAL TIME - pass the "
        "hour they said, do not convert to UTC. For a single future run, pass run_at as "
        "'YYYY-MM-DDTHH:MM' local (an explicit offset or trailing Z is honoured if given). "
        "For a fixed interval such as every five minutes, pass interval_minutes. "
        "For a repeating wall-clock run, pass hour (0-23) plus optional minute (0-59) and days (omit "
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
            "label": {
                "type": "string",
                "description": (
                    "A short title for this schedule, 2-4 words, e.g. 'Morning stretch'. "
                    "Shown in lists; the prompt in 'task' is what actually runs."
                ),
            },
            "agent": {
                "type": "string",
                "description": "Agent to run it (default 'general')",
                "default": "general",
            },
            "skill": {
                "type": "string",
                "description": "Optional reusable skill/playbook to apply when the task runs",
            },
            "flow": {
                "type": "string",
                "description": "Optional declarative flow to execute when the schedule fires",
            },
            "run_at": {"type": "string", "description": "Local ISO 8601 datetime for a one-shot run"},
            "hour": {"type": "integer", "description": "Hour (0-23), local, for a recurring schedule"},
            "minute": {"type": "integer", "description": "Minute (0-59, default 0)"},
            "interval_minutes": {
                "type": "integer",
                "description": "Run every N minutes, starting when the schedule is created (minimum 1).",
            },
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

    def __init__(
        self,
        job_processor,
        cron_store,
        skill_registry: SkillRegistry | None = None,
        flow_registry: FlowRegistry | None = None,
        agent_registry: AgentRegistry | None = None,
        tool_registry=None,
    ) -> None:
        self._job_processor = job_processor
        self._cron_store = cron_store
        self._skill_registry = skill_registry
        self._flow_registry = flow_registry
        self._agent_registry = agent_registry
        self._tool_registry = tool_registry

    async def run(self, input: ToolInput) -> ToolOutput:
        task = str(input.params.get("task", "")).strip()
        if not task:
            return ToolOutput(success=False, error="Parameter 'task' is required.")

        agent = str(input.params.get("agent", "general"))
        if self._agent_registry is not None and agent not in self._agent_registry.names():
            return ToolOutput(success=False, error=f"Unknown agent '{agent}'.")
        skill = str(input.params.get("skill", "")).strip()
        flow = str(input.params.get("flow", "")).strip()
        if skill and self._skill_registry is not None:
            from skills.exceptions import SkillNotFoundError

            try:
                selected_skill = self._skill_registry.get(skill)
            except SkillNotFoundError:
                return ToolOutput(success=False, error=f"Unknown skill '{skill}'.")
            if selected_skill.status != "active":
                return ToolOutput(success=False, error=f"Skill '{skill}' is {selected_skill.status}, not active.")
        if flow and self._flow_registry is not None:
            from flows.exceptions import FlowNotFoundError

            try:
                selected_flow = self._flow_registry.get(flow)
            except FlowNotFoundError:
                return ToolOutput(success=False, error=f"Unknown flow '{flow}'.")
            if selected_flow.status != "active":
                return ToolOutput(success=False, error=f"Flow '{flow}' is {selected_flow.status}, not active.")
            if selected_flow.activation_fingerprint and self._skill_registry is not None:
                from flows.models import flow_fingerprint

                if selected_flow.activation_fingerprint != flow_fingerprint(
                    selected_flow, self._skill_registry.get
                ):
                    return ToolOutput(
                        success=False,
                        error=f"Flow '{flow}' changed after activation; test and activate it again.",
                    )
            from tools.universal._flow_validation import validate_flow_capabilities

            report = validate_flow_capabilities(
                selected_flow,
                skill_registry=self._skill_registry,
                agent_registry=self._agent_registry,
                tool_registry=self._tool_registry,
            )
            if not report.valid:
                return ToolOutput(
                    success=False,
                    error=f"Flow '{flow}' is no longer executable: {'; '.join(report.errors)}",
                )
        run_at = input.params.get("run_at")
        hour = input.params.get("hour")
        interval_minutes = input.params.get("interval_minutes")

        modes = sum(value is not None for value in (run_at, hour, interval_minutes))
        if modes != 1:
            return ToolOutput(
                success=False,
                error="Provide exactly one of 'run_at', 'hour', or 'interval_minutes'.",
            )

        if run_at is not None:
            return await self._one_shot(task, agent, str(run_at), skill, flow)
        if interval_minutes is not None:
            return await self._interval(task, agent, input.params, skill, flow)
        if hour is not None:
            return await self._recurring(task, agent, input.params, skill, flow)
        return ToolOutput(
            success=False,
            error="Provide 'run_at', 'hour', or 'interval_minutes'.",
        )

    async def _interval(
        self,
        task: str,
        agent: str,
        params: dict,
        skill: str = "",
        flow: str = "",
    ) -> ToolOutput:
        try:
            interval = int(params["interval_minutes"])
            if interval < 1:
                raise ValueError("interval_minutes must be at least 1")
            label = str(params.get("label", "")).strip()
            anchor = now_epoch()
            entry = CronEntry(
                name=await self._cron_store.unique_name(label or task),
                agent=agent,
                task=task,
                label=label,
                hour=0,
                minute=0,
                interval_minutes=interval,
                anchor_epoch=anchor,
                skill=skill,
                flow=flow,
            )
        except (TypeError, ValueError) as exc:
            return ToolOutput(success=False, error=str(exc))

        await self._cron_store.add(
            name=entry.name,
            agent=entry.agent,
            task=entry.task,
            hour=entry.hour,
            minute=entry.minute,
            weekdays=None,
            tz=entry.tz,
            label=entry.label,
            skill=entry.skill,
            flow=entry.flow,
            interval_minutes=entry.interval_minutes,
            anchor_epoch=entry.anchor_epoch,
        )
        row = await self._cron_store.get(entry.name)
        return ToolOutput(success=True, data={"type": "recurring", **entry_view(row)})

    async def _one_shot(self, task: str, agent: str, run_at: str, skill: str = "", flow: str = "") -> ToolOutput:
        try:
            epoch = parse_local(run_at)
        except ValueError as exc:
            return ToolOutput(success=False, error=f"Invalid run_at: {exc}")

        job = Job(
            job_id=generate_id(),
            type=JobType.ASYNC,
            agent=agent,
            task=task,
            payload={
                "scheduled_by": "schedule_task",
                **({"skill": skill} if skill else {}),
                **({"flow": flow} if flow else {}),
            },
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

    async def _recurring(self, task: str, agent: str, params: dict, skill: str = "", flow: str = "") -> ToolOutput:
        from jobs.scheduler import CronEntry

        # `weekday` is the old single-day spelling; still read so a caller working
        # from a cached description of this tool is not simply refused.
        days = params.get("days", params.get("weekday"))
        try:
            tz = resolve_zone_name(params.get("tz"))
            label = str(params.get("label", "")).strip()
            entry = CronEntry(
                name=await self._cron_store.unique_name(label or task),
                agent=agent,
                task=task,
                label=label,
                hour=_whole_number(params, "hour", 23),
                minute=_whole_number(params, "minute", 59, default=0),
                weekdays=parse_weekdays(days),
                tz=tz,
                skill=skill,
                flow=flow,
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
            label=entry.label,
            skill=entry.skill,
            flow=entry.flow,
        )
        row = await self._cron_store.get(entry.name)
        return ToolOutput(success=True, data={"type": "recurring", **entry_view(row)})

    def format_output(self, data: dict) -> str:
        if data.get("type") == "one-shot":
            return f"Scheduled once: {data['task']} - runs {data['runs_at']} (job {data['job_id']})."
        return f"Scheduled {data['schedule']}: {data['title']} - next run {data['next_run']}."
