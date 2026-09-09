"""Tool that changes an existing recurring schedule."""

from __future__ import annotations

from jobs.scheduler import builtin_default
from tools.base import Tool
from tools.models import ToolInput, ToolOutput
from tools.universal._schedules import entry_view, parse_weekdays, resolve_zone_name


class UpdateScheduleTool(Tool):
    name = "update_schedule"
    excluded_domains = frozenset({"engineering"})
    description = (
        "Change any recurring schedule - the user's own or one north ships with: its "
        "time, its days, the task it runs, the agent that runs it, or whether it is "
        "paused. Address it by the 'name' shown by "
        "list_schedules, and pass only the fields that change - anything omitted is left "
        "alone. Times are the user's local time. 'days' takes day names or numbers "
        "(0=Mon … 6=Sun), or 'weekdays' / 'weekends' / 'daily'; pass 'daily' to go back to "
        "running every day. Pass enabled false to pause a schedule without losing it, and "
        "true to resume it. To move a one-shot task instead, cancel it and schedule a new "
        "one. To remove a schedule for good, use cancel_schedule."
    )
    is_mutating = True
    parameters_schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Schedule name from list_schedules"},
            "task": {"type": "string", "description": "New task prompt"},
            "label": {"type": "string", "description": "New short title shown in lists"},
            "agent": {"type": "string", "description": "New agent to run it"},
            "hour": {"type": "integer", "description": "New hour (0-23), local"},
            "minute": {"type": "integer", "description": "New minute (0-59)"},
            "days": {
                "description": (
                    "New days: names or numbers (0=Mon…6=Sun), or 'weekdays' / 'weekends' / "
                    "'daily'. Pass 'daily' to clear a day restriction."
                ),
            },
            "enabled": {"type": "boolean", "description": "false pauses the schedule, true resumes it"},
            "tz": {"type": "string", "description": "New IANA zone"},
        },
        "required": ["name"],
    }

    def __init__(self, cron_store) -> None:
        self._cron_store = cron_store

    async def run(self, input: ToolInput) -> ToolOutput:
        name = str(input.params.get("name", "")).strip()
        if not name:
            return ToolOutput(success=False, error="Parameter 'name' is required.")
        if await self._cron_store.get(name) is None and not await self._seed_builtin(name):
            return ToolOutput(
                success=False,
                error=f"No schedule named {name!r}. Call list_schedules to see what exists.",
            )
        try:
            changes = self._changes(input.params)
        except (TypeError, ValueError) as exc:
            return ToolOutput(success=False, error=str(exc))

        await self._cron_store.update(name, **changes)
        row = await self._cron_store.get(name)
        return ToolOutput(success=True, data={"changed": sorted(changes), **entry_view(row)})

    async def _seed_builtin(self, name: str) -> bool:
        """Write a stored row for a built-in so an edit has somewhere to land.

        Seeded from the shipped values, so an edit naming one field leaves the
        rest alone, and cancel_schedule can restore the default by deleting it.
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
        for field in ("task", "agent", "label"):
            if params.get(field) is not None:
                changes[field] = str(params[field])
        for field, ceiling in (("hour", 23), ("minute", 59)):
            if params.get(field) is None:
                continue
            value = int(params[field])
            if not 0 <= value <= ceiling:
                raise ValueError(f"{field} must be in [0, {ceiling}], got {value}")
            changes[field] = value
        days = params.get("days", params.get("weekday"))
        if days is not None:
            changes["weekdays"] = parse_weekdays(days)
        if params.get("enabled") is not None:
            changes["enabled"] = bool(params["enabled"])
        if params.get("tz") is not None:
            changes["tz"] = resolve_zone_name(str(params["tz"]))
        return changes

    def format_output(self, data: dict) -> str:
        state = "" if data.get("enabled", True) else " (paused)"
        if not data.get("changed"):
            return f"{data['name']} is unchanged: {data['schedule']}{state}, next run {data['next_run']}."
        changed = ", ".join(data["changed"])
        return f"Updated {data['name']} ({changed}): now {data['schedule']}{state}, next run {data['next_run']}."
