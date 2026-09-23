"""Tool that changes an existing recurring schedule."""

from __future__ import annotations

from typing import TYPE_CHECKING

from jobs.scheduler import builtin_default
from tools.base import Tool
from tools.models import ToolInput, ToolOutput
from tools.universal._schedules import entry_view, parse_weekdays, resolve_zone_name
from utils.time import now_epoch

if TYPE_CHECKING:
    from agents.registry import AgentRegistry
    from flows.registry import FlowRegistry
    from skills.registry import SkillRegistry


class UpdateScheduleTool(Tool):
    name = "update_schedule"
    description = (
        "Change any recurring schedule - the user's own or one north ships with: its "
        "time, its days, the task it runs, the agent that runs it, or whether it is "
        "paused. Address it by the 'name' shown by "
        "list_schedules, and pass only the fields that change - anything omitted is left "
        "alone. Times are the user's local time. 'days' takes day names or numbers "
        "(0=Mon … 6=Sun), or 'weekdays' / 'weekends' / 'daily'; pass 'daily' to go back to "
        "running every day. Pass interval_minutes to change it to a fixed interval, or pass "
        "hour to change an interval back to a wall-clock schedule. Pass enabled false to "
        "pause a schedule without losing it, and "
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
            "interval_minutes": {
                "type": "integer",
                "description": "Change to every N minutes, anchored when this update is made.",
            },
            "days": {
                "description": (
                    "New days: names or numbers (0=Mon…6=Sun), or 'weekdays' / 'weekends' / "
                    "'daily'. Pass 'daily' to clear a day restriction."
                ),
            },
            "enabled": {"type": "boolean", "description": "false pauses the schedule, true resumes it"},
            "tz": {"type": "string", "description": "New IANA zone"},
            "skill": {"type": "string", "description": "Reusable skill/playbook to apply when it runs"},
            "flow": {"type": "string", "description": "Declarative flow to execute when it runs"},
        },
        "required": ["name"],
    }

    def __init__(
        self,
        cron_store,
        skill_registry: SkillRegistry | None = None,
        flow_registry: FlowRegistry | None = None,
        agent_registry: AgentRegistry | None = None,
        tool_registry=None,
    ) -> None:
        self._cron_store = cron_store
        self._skill_registry = skill_registry
        self._flow_registry = flow_registry
        self._agent_registry = agent_registry
        self._tool_registry = tool_registry

    async def run(self, input: ToolInput) -> ToolOutput:
        name = str(input.params.get("name", "")).strip()
        if not name:
            return ToolOutput(success=False, error="Parameter 'name' is required.")
        reference_error = self._reference_error(input.params)
        if reference_error:
            return ToolOutput(success=False, error=reference_error)
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

    def _reference_error(self, params: dict) -> str:
        agent_name = str(params.get("agent") or "").strip()
        if agent_name and self._agent_registry is not None and agent_name not in self._agent_registry.names():
            return f"Unknown agent '{agent_name}'."
        skill_name = str(params.get("skill") or "").strip()
        if skill_name and self._skill_registry is not None:
            try:
                skill = self._skill_registry.get(skill_name)
            except Exception:
                return f"Unknown skill '{skill_name}'."
            if skill.status != "active":
                return f"Skill '{skill_name}' is {skill.status}, not active."
        flow_name = str(params.get("flow") or "").strip()
        if flow_name and self._flow_registry is not None:
            try:
                flow = self._flow_registry.get(flow_name)
            except Exception:
                return f"Unknown flow '{flow_name}'."
            if flow.status != "active":
                return f"Flow '{flow_name}' is {flow.status}, not active."
            if flow.activation_fingerprint and self._skill_registry is not None:
                from flows.models import flow_fingerprint

                if flow.activation_fingerprint != flow_fingerprint(flow, self._skill_registry.get):
                    return f"Flow '{flow_name}' changed after activation; test and activate it again."
            from tools.universal._flow_validation import validate_flow_capabilities

            report = validate_flow_capabilities(
                flow,
                skill_registry=self._skill_registry,
                agent_registry=self._agent_registry,
                tool_registry=self._tool_registry,
            )
            if not report.valid:
                return f"Flow '{flow_name}' is no longer executable: {'; '.join(report.errors)}"
        return ""

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
        for field in ("task", "agent", "label", "skill", "flow"):
            if params.get(field) is not None:
                changes[field] = str(params[field])
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
            changes["interval_minutes"] = interval
            changes["anchor_epoch"] = now_epoch()
            changes["hour"] = 0
            changes["minute"] = 0
            changes["weekdays"] = None
        elif params.get("hour") is not None:
            changes["interval_minutes"] = None
            changes["anchor_epoch"] = None
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
