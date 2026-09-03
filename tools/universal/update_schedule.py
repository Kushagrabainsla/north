"""Tool that changes an existing recurring schedule."""

from __future__ import annotations

from tools.base import Tool
from tools.models import ToolInput, ToolOutput
from tools.universal._schedules import BUILTIN_NAMES, entry_view, resolve_zone_name


class UpdateScheduleTool(Tool):
    name = "update_schedule"
    excluded_domains = frozenset({"engineering"})
    description = (
        "Change an existing recurring schedule: its time, its day, the task it runs, or "
        "the agent that runs it. Address it by the 'name' shown by list_schedules, and "
        "pass only the fields that change - anything omitted is left alone. Times are the "
        "user's local time. To move a one-shot task instead, cancel it and schedule a new "
        "one. To stop a schedule entirely, use cancel_schedule."
    )
    is_mutating = True
    parameters_schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Schedule name from list_schedules"},
            "task": {"type": "string", "description": "New task prompt"},
            "agent": {"type": "string", "description": "New agent to run it"},
            "hour": {"type": "integer", "description": "New hour (0-23), local"},
            "minute": {"type": "integer", "description": "New minute (0-59)"},
            "weekday": {"type": "integer", "description": "New weekday 0=Mon…6=Sun"},
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
        if name in BUILTIN_NAMES:
            return ToolOutput(success=False, error=f"{name!r} is a built-in schedule and cannot be changed.")
        if await self._cron_store.get(name) is None:
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

    @staticmethod
    def _changes(params: dict) -> dict[str, object]:
        """Pick out the fields the caller actually set, validated and typed."""
        changes: dict[str, object] = {}
        for field in ("task", "agent"):
            if params.get(field) is not None:
                changes[field] = str(params[field])
        for field, ceiling in (("hour", 23), ("minute", 59), ("weekday", 6)):
            if params.get(field) is None:
                continue
            value = int(params[field])
            if not 0 <= value <= ceiling:
                raise ValueError(f"{field} must be in [0, {ceiling}], got {value}")
            changes[field] = value
        if params.get("tz") is not None:
            changes["tz"] = resolve_zone_name(str(params["tz"]))
        return changes

    def format_output(self, data: dict) -> str:
        if not data.get("changed"):
            return f"{data['name']} is unchanged: {data['schedule']}, next run {data['next_run']}."
        changed = ", ".join(data["changed"])
        return f"Updated {data['name']} ({changed}): now {data['schedule']}, next run {data['next_run']}."
