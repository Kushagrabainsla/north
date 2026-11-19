"""A flow's schedules: when north runs it, once or again and again.

A schedule is a property of a flow. A reminder is a schedule of the built-in
reminder flow, with its text as the flow's input. The chat reaches these
through `create_flow` (schedule, reschedule, unschedule; list and read show
each flow's schedules) and the dashboard through its flow routes, so both doors
obey one set of rules.

It was four tools beside the flow tools, for what is one property of a flow.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from agents.constants import NORTH_AGENT
from flows.validation import schedulable_flow_error
from jobs.models import REMINDER_FLOW, Job, JobPriority, JobType
from jobs.scheduler import CronEntry, builtin_default
from tools.models import ToolOutput
from tools.universal._schedules import (
    BUILTIN_NAMES,
    TIMEZONE_INPUT_ERROR,
    builtin_views,
    entry_view,
    is_pending_one_shot,
    job_view,
    parse_weekdays,
)
from utils.ids import generate_id
from utils.time import format_local, from_epoch, local_timezone_name, now_epoch, parse_local

if TYPE_CHECKING:
    from agents.registry import AgentRegistry
    from flows.registry import FlowRegistry
    from skills.registry import SkillRegistry

NO_FLOW = (
    "Pass 'message' for a reminder: the text north delivers at that time. Pass the flow's 'name' for work "
    "north must do then. To schedule work that is not a flow yet, create it first, test it, and schedule "
    "it once the user confirms."
)
BOTH = "Pass either 'message' (a reminder) or the name of another flow (work to do), not both."
ONE_TIMING = "Provide exactly one of 'run_at', 'hour', or 'interval_minutes'."


def _dashboard_only() -> list[str]:
    return ["the dashboard"]


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


class FlowSchedules:
    """Add, show, change and remove the schedules of flows."""

    def __init__(
        self,
        job_processor,
        cron_store,
        *,
        skill_registry: SkillRegistry | None = None,
        flow_registry: FlowRegistry | None = None,
        agent_registry: AgentRegistry | None = None,
        tool_registry=None,
        reaches: Callable[[], list[str]] = _dashboard_only,
    ) -> None:
        self._job_processor = job_processor
        self._cron_store = cron_store
        self._skill_registry = skill_registry
        self._flow_registry = flow_registry
        self._agent_registry = agent_registry
        self._tool_registry = tool_registry
        # Where a delivered reminder will appear: the dashboard always, other channels when set up.
        self._reaches = reaches

    # -- add ---------------------------------------------------------------

    async def add(self, params: dict[str, Any]) -> ToolOutput:
        """Put *params['flow']* on a schedule; a *message* makes it a reminder."""
        if {"tz", "timezone"} & params.keys():
            return ToolOutput(success=False, error=TIMEZONE_INPUT_ERROR)
        flow = str(params.get("flow") or "").strip()
        message = str(params.get("message") or "").strip()
        if message and flow not in {"", REMINDER_FLOW}:
            return ToolOutput(success=False, error=BOTH)
        if flow == REMINDER_FLOW and not message:
            return ToolOutput(success=False, error="A reminder needs its 'message': the text to deliver.")
        if message:
            flow = REMINDER_FLOW
        if not flow:
            return ToolOutput(success=False, error=NO_FLOW)
        label = str(params.get("label") or "").strip()
        if message:
            # A reminder is its text: the reminder flow delivers it as it is.
            task = label or message
        else:
            description, problem = self._schedulable(flow)
            if problem:
                return ToolOutput(success=False, error=problem)
            task = str(params.get("task") or "").strip() or description or f"Run the {flow} flow."
        timings = [field for field in ("run_at", "hour", "interval_minutes") if params.get(field) is not None]
        if len(timings) != 1:
            return ToolOutput(success=False, error=ONE_TIMING)
        try:
            if timings == ["run_at"]:
                output = await self._once(task, flow, message, label, str(params["run_at"]))
            else:
                output = await self._repeating(task, flow, message, label, params)
        except ValueError as exc:
            return ToolOutput(success=False, error=str(exc))
        if output.success and message and output.data is not None:
            output.data["delivered_to"] = self._reaches()
        return output

    def _schedulable(self, flow_name: str) -> tuple[str, str]:
        """The flow's description, and why it cannot be scheduled ("" when it can)."""
        if self._flow_registry is None:
            return "", ""
        from flows.exceptions import FlowNotFoundError

        try:
            flow = self._flow_registry.get(flow_name)
        except FlowNotFoundError:
            return "", f"Unknown flow '{flow_name}'."
        problem = schedulable_flow_error(
            flow,
            skill_registry=self._skill_registry,
            agent_registry=self._agent_registry,
            tool_registry=self._tool_registry,
        )
        return flow.description, problem

    async def _once(self, task: str, flow: str, message: str, label: str, run_at: str) -> ToolOutput:
        try:
            epoch = parse_local(run_at)
        except ValueError as exc:
            raise ValueError(f"Invalid run_at: {exc}") from None
        job = Job(
            job_id=generate_id(),
            type=JobType.ASYNC,
            agent=NORTH_AGENT,
            task=task,
            payload={
                "scheduled_by": "create_flow",
                "flow": flow,
                **({"message": message, "label": label or "Reminder"} if message else {}),
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
                "flow": flow,
                "runs_at": format_local(epoch),
                "runs_at_epoch": epoch,
                "task": task,
            },
        )

    async def _repeating(self, task: str, flow: str, message: str, label: str, params: dict) -> ToolOutput:
        name = await self._cron_store.unique_name(label or task)
        if params.get("interval_minutes") is not None:
            interval = int(params["interval_minutes"])
            if interval < 1:
                raise ValueError("interval_minutes must be at least 1")
            timing: dict[str, Any] = {
                "hour": 0,
                "minute": 0,
                "weekdays": None,
                "interval_minutes": interval,
                "anchor_epoch": now_epoch(),
            }
        else:
            # `weekday` is the old single-day spelling, still read for a caller working from an old description.
            timing = {
                "hour": _whole_number(params, "hour", 23),
                "minute": _whole_number(params, "minute", 59, default=0),
                "weekdays": parse_weekdays(params.get("days", params.get("weekday"))),
            }
        entry = CronEntry(name=name, agent=NORTH_AGENT, task=task, label=label, flow=flow, message=message, **timing)
        await self._cron_store.add(
            name=entry.name,
            agent=entry.agent,
            task=entry.task,
            hour=entry.hour,
            minute=entry.minute,
            weekdays=entry.weekdays,
            tz=entry.tz,
            label=entry.label,
            flow=entry.flow,
            interval_minutes=entry.interval_minutes,
            anchor_epoch=entry.anchor_epoch,
            message=entry.message,
        )
        return ToolOutput(success=True, data={"type": "recurring", **entry_view(await self._cron_store.get(name))})

    # -- show --------------------------------------------------------------

    async def by_flow(self) -> dict[str, list[dict[str, Any]]]:
        """Every schedule, the user's and north's own, grouped under the flow it runs, soonest first."""
        views = [entry_view(row) for row in await self._cron_store.list()]
        stored = {view["name"] for view in views}
        # A built-in that was edited has a stored row; that row is the one that fires.
        views += [view for view in builtin_views() if view["name"] not in stored]
        views += [job_view(job) for job in await self._job_processor.list_jobs(limit=200) if is_pending_one_shot(job)]
        grouped: dict[str, list[dict[str, Any]]] = {}
        for view in sorted(views, key=lambda item: item["next_run_epoch"] or float("inf")):
            grouped.setdefault(view.get("flow") or "", []).append(view)
        return grouped

    @staticmethod
    def timezone() -> str:
        return local_timezone_name()

    # -- change ------------------------------------------------------------

    async def change(self, params: dict[str, Any]) -> ToolOutput:
        """Retime, rename, pause or resume the repeating schedule *params['schedule']*."""
        if {"tz", "timezone"} & params.keys():
            return ToolOutput(success=False, error=TIMEZONE_INPUT_ERROR)
        name = str(params.get("schedule") or "").strip()
        if not name:
            return ToolOutput(success=False, error="Parameter 'schedule' is required: the schedule's name.")
        if await self._cron_store.get(name) is None and not await self._seed_builtin(name):
            return ToolOutput(success=False, error=f"No schedule named {name!r}. List the flows to see what exists.")
        try:
            changes = self._changes(params)
        except (TypeError, ValueError) as exc:
            return ToolOutput(success=False, error=str(exc))
        await self._cron_store.update(name, **changes)
        return ToolOutput(
            success=True, data={"changed": sorted(changes), **entry_view(await self._cron_store.get(name))}
        )

    async def _seed_builtin(self, name: str) -> bool:
        """Write a stored row for a built-in so an edit has somewhere to land.

        Seeded from the shipped values, so an edit naming one field leaves the
        rest alone, and unschedule can restore the default by deleting it.
        """
        default = builtin_default(name)
        if default is None:
            return False
        await self._cron_store.add(
            name=default.name,
            agent=default.agent,
            task=default.task,
            hour=default.hour,
            minute=default.minute,
            weekdays=default.weekdays,
            tz=default.zone_name,
            enabled=default.enabled,
            label=default.label,
            skill=default.skill,
            flow=default.flow,
            interval_minutes=default.interval_minutes,
            anchor_epoch=default.anchor_epoch,
        )
        return True

    @staticmethod
    def _changes(params: dict) -> dict[str, object]:
        """Pick out the fields the caller actually set, validated and typed.

        A field the caller did not mention is absent from the result, not None:
        "leave the days alone" and "clear the days back to daily" are different
        requests, and collapsing both to None made the second one impossible.
        """
        changes: dict[str, object] = {}
        if params.get("label") is not None:
            changes["label"] = str(params["label"])
        for field, ceiling in (("hour", 23), ("minute", 59)):
            if params.get(field) is None:
                continue
            value = int(params[field])
            if not 0 <= value <= ceiling:
                raise ValueError(f"{field} must be in [0, {ceiling}], got {value}")
            changes[field] = value
        if params.get("interval_minutes") is not None:
            if any(params.get(field) is not None for field in ("hour", "minute", "days", "weekday")):
                raise ValueError("interval_minutes cannot be combined with hour, minute, or days")
            interval = int(params["interval_minutes"])
            if interval < 1:
                raise ValueError(f"interval_minutes must be at least 1, got {interval}")
            changes.update(interval_minutes=interval, anchor_epoch=now_epoch(), hour=0, minute=0, weekdays=None)
        elif params.get("hour") is not None:
            changes["interval_minutes"] = None
            changes["anchor_epoch"] = None
        days = params.get("days", params.get("weekday"))
        if days is not None:
            changes["weekdays"] = parse_weekdays(days)
        if params.get("enabled") is not None:
            changes["enabled"] = bool(params["enabled"])
        return changes

    # -- remove ------------------------------------------------------------

    async def remove(self, params: dict[str, Any]) -> ToolOutput:
        """Delete the repeating schedule *params['schedule']*, or cancel the one-shot *params['job_id']*."""
        name = str(params.get("schedule") or "").strip()
        job_id = str(params.get("job_id") or "").strip()
        if not name and not job_id:
            return ToolOutput(success=False, error="Provide 'schedule' (repeating) or 'job_id' (one-shot).")
        if name:
            if await self._cron_store.remove(name):
                # Removing the stored row for a built-in undoes the edit, not the schedule.
                kind = "restored" if name in BUILTIN_NAMES else "recurring"
                return ToolOutput(success=True, data={"cancelled": name, "type": kind})
            if name in BUILTIN_NAMES:
                return ToolOutput(
                    success=False,
                    error=f"{name!r} ships with north, so there is nothing to delete. Pause it (enabled false).",
                )
            return ToolOutput(success=False, error=f"No schedule named {name!r}. List the flows to see what exists.")
        job = await self._job_processor.get(job_id)
        if job is None:
            return ToolOutput(success=False, error=f"No job {job_id!r}.")
        await self._job_processor.cancel(job_id)
        return ToolOutput(success=True, data={"cancelled": job_id, "type": "one-shot", "task": job.task})


def describe_schedule(view: dict[str, Any]) -> str:
    """One schedule on one line, as the chat shows it under its flow."""
    if view.get("job_id"):
        return f"{view['job_id']}  once  {view['next_run']}  - {view['task']}"
    state = "" if view.get("enabled", True) else " (paused)"
    builtin = " [built-in]" if view.get("source") == "builtin" else ""
    return f"{view['name']}  {view['schedule']}{state}  next {view['next_run']}  - {view['title']}{builtin}"


def describe_result(data: dict[str, Any]) -> str:
    """What a schedule, reschedule or unschedule did, in one sentence."""
    if "cancelled" in data:
        if data["type"] == "restored":
            return f"{data['cancelled']} is back to the settings north ships with."
        return f"Cancelled {data['type']} schedule {data['cancelled']} - it will not run again."
    if "changed" in data:
        state = "" if data.get("enabled", True) else " (paused)"
        if not data["changed"]:
            return f"{data['name']} is unchanged: {data['schedule']}{state}, next run {data['next_run']}."
        changed = ", ".join(data["changed"])
        return f"Updated {data['name']} ({changed}): now {data['schedule']}{state}, next run {data['next_run']}."
    where = f" It will appear on {' and on '.join(data['delivered_to'])}." if data.get("delivered_to") else ""
    if data.get("type") == "one-shot":
        return f"Scheduled once: {data['task']} - runs {data['runs_at']} (job {data['job_id']}).{where}"
    return f"Scheduled {data['schedule']}: {data['title']} - next run {data['next_run']}.{where}"
