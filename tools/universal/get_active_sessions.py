"""GetActiveSessionsTool - discover other running sessions and their domains.

Agents use this tool to find out what other tasks are currently running --
their domain, description, and how long they've been active. This enables
coordination between sessions (e.g. a health agent checking whether the
finance agent is also running before making recommendations).

See docs/CODING_STYLE.md Section 16.1.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from tools import Tool, ToolInput, ToolOutput

if TYPE_CHECKING:
    from orchestrator.running_tasks import RunningTaskStore


class GetActiveSessionsTool(Tool):
    """Discover other running sessions/tasks and their current status."""

    name = "get_active_sessions"
    excluded_domains = frozenset({"engineering"})
    description = (
        "List every other task (session) that is currently running right now, "
        "including their domain, description, and how long they've been active. "
        "Use this to check what else is happening concurrently before making "
        "recommendations or decisions that might overlap with another session. "
        "Your own task is excluded from the results."
    )
    parameters_schema = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    def __init__(self, running_task_store: RunningTaskStore | None = None) -> None:
        self._store = running_task_store

    def format_output(self, data: dict[str, Any]) -> str:
        sessions = data.get("sessions", [])
        if not sessions:
            return "No other sessions are currently running."

        lines = [f"## Active sessions ({len(sessions)})"]
        for s in sessions:
            age = _format_age(s.get("started_at", ""))
            desc = s.get("description", "")
            desc_suffix = f" — {desc}" if desc else ""
            lines.append(f"- `{s['task_id']}` [{s['domain']}]{desc_suffix} (started {age})")
        return "\n".join(lines)

    async def run(self, input: ToolInput) -> ToolOutput:
        if self._store is None:
            return ToolOutput(success=False, error="Active sessions tool not connected to running task store.")

        task_id = input.params.get("task_id", "")
        try:
            sessions = await self._store.list_active(exclude_task_id=task_id or None)
        except Exception as exc:
            return ToolOutput(success=False, error=f"Failed to query active sessions: {exc}")

        return ToolOutput(success=True, data={"sessions": sessions})


def _format_age(started_at: str) -> str:
    """Return a human-friendly age string like '2m ago'."""
    from datetime import UTC, datetime

    try:
        start = datetime.fromisoformat(started_at)
        delta = datetime.now(UTC) - start
        seconds = int(delta.total_seconds())
        if seconds < 60:
            return f"{seconds}s ago"
        if seconds < 3600:
            return f"{seconds // 60}m ago"
        if seconds < 86400:
            return f"{seconds // 3600}h ago"
        return f"{seconds // 86400}d ago"
    except (ValueError, TypeError):
        return started_at
