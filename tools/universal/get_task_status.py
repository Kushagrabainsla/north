"""GetTaskStatusTool - query the real-time status of a task from the ledger.

Agents must use this tool to answer questions about whether a task or
sub-agent is still running rather than inferring from memory.

See docs/CODING_STYLE.md Section 16.1.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from tools import Tool, ToolInput, ToolOutput

if TYPE_CHECKING:
    pass


class GetTaskStatusTool(Tool):
    """Look up the latest ledger status for a given task_id."""

    name = "get_task_status"
    description = (
        "Look up the actual status of a task from the audit ledger. "
        "Use this whenever asked whether a task, delegation, or sub-agent is "
        "still running — never guess from memory. Returns the task's latest "
        "action, status, agent, and timestamp."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "The task ID to query (e.g. 'task_651d7937d08c').",
            },
        },
        "required": ["task_id"],
    }

    def __init__(self, ledger=None) -> None:
        self._ledger = ledger

    def format_output(self, data: dict[str, Any]) -> str:
        if not data.get("found"):
            return f"No ledger entries found for task `{data.get('task_id', '?')}`."

        lines = [f"## Task Status: `{data['task_id']}`\n"]
        lines.append(f"- **Latest action**: `{data.get('action', 'unknown')}`")
        lines.append(f"- **Status**: `{data.get('status', 'unknown')}`")
        lines.append(f"- **Agent**: `{data.get('agent', 'unknown')}`")
        lines.append(f"- **Timestamp**: {data.get('timestamp', 'unknown')}")
        if data.get("output"):
            lines.append(f"- **Output summary**: {data['output'][:300]}")
        lines.append(f"\n**Total ledger entries for this task**: {data.get('total_entries', 0)}")
        return "\n".join(lines)

    async def run(self, input: ToolInput) -> ToolOutput:
        if self._ledger is None:
            return ToolOutput(success=False, error="Task status tool not connected to ledger.")

        task_id = input.params.get("task_id", "").strip()
        if not task_id:
            return ToolOutput(success=False, error="Parameter 'task_id' is required.")

        try:
            from ledger.base import LedgerFilters

            entries = await self._ledger.query(LedgerFilters(task_id=task_id, limit=100))
        except Exception as exc:
            return ToolOutput(success=False, error=f"Ledger query failed: {exc}")

        if not entries:
            return ToolOutput(
                success=True,
                data={"found": False, "task_id": task_id},
            )

        # Entries are returned newest-first by the ledger query contract.
        latest = entries[0]
        return ToolOutput(
            success=True,
            data={
                "found": True,
                "task_id": task_id,
                "action": latest.action,
                "status": latest.status,
                "agent": latest.agent,
                "timestamp": latest.timestamp.isoformat() if latest.timestamp else None,
                "output": latest.output,
                "total_entries": len(entries),
            },
        )
