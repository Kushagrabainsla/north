"""GetTaskStatusTool - query the real-time status of a task from the ledger.

Agents must use this tool to answer questions about whether a task or
sub-agent is still running rather than inferring from memory.

See docs/CODING_STYLE.md Section 16.1.
"""

from __future__ import annotations

from typing import Any

from tools import Tool, ToolInput, ToolOutput


class GetTaskStatusTool(Tool):
    """Look up the latest ledger status for a task, or list running tasks."""

    name = "get_task_status"
    description = (
        "Look up the actual status of a task from the audit ledger. "
        "Use this whenever asked whether a task, delegation, or sub-agent is "
        "still running — never guess from memory. Pass a 'task_id' to query a "
        "specific task; omit it to list every task that is currently running. "
        "Returns each task's latest action, status, agent, and timestamp."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": (
                    "The task ID to query (e.g. 'task_651d7937d08c'). "
                    "Omit to list all tasks that are currently running - use this "
                    "when you do not have the id of an earlier task."
                ),
            },
        },
        "required": [],
    }

    def __init__(self, ledger=None) -> None:
        self._ledger = ledger

    def format_output(self, data: dict[str, Any]) -> str:
        if data.get("mode") == "running":
            running = data.get("running", [])
            if not running:
                return (
                    "No tasks are currently running. Delegation is synchronous, "
                    "so nothing is in progress in the background."
                )
            lines = [f"## Currently running tasks ({len(running)})\n"]
            for t in running:
                lines.append(
                    f"- `{t['task_id']}` — agent `{t.get('agent', 'unknown')}`, "
                    f"latest action `{t.get('action', 'unknown')}` at {t.get('timestamp', 'unknown')}"
                )
            return "\n".join(lines)

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

        task_id = (input.params.get("task_id") or "").strip()
        if not task_id:
            return await self._list_running()
        return await self._query_one(task_id)

    async def _query_one(self, task_id: str) -> ToolOutput:
        try:
            from ledger.base import LedgerFilters

            entries = await self._ledger.query(LedgerFilters(task_id=task_id, limit=100))
        except Exception as exc:
            return ToolOutput(success=False, error=f"Ledger query failed: {exc}")

        if not entries:
            return ToolOutput(success=True, data={"found": False, "task_id": task_id})

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

    async def _list_running(self) -> ToolOutput:
        """List tasks whose latest ledger entry is still PENDING (i.e. running)."""
        try:
            from ledger.base import LedgerFilters

            task_ids = await self._ledger.pending_task_ids(limit=50)
            running = []
            for tid in task_ids:
                entries = await self._ledger.query(LedgerFilters(task_id=tid, limit=1))
                latest = entries[0] if entries else None
                running.append(
                    {
                        "task_id": tid,
                        "agent": latest.agent if latest else None,
                        "action": latest.action if latest else None,
                        "timestamp": latest.timestamp.isoformat() if latest and latest.timestamp else None,
                    }
                )
        except Exception as exc:
            return ToolOutput(success=False, error=f"Ledger query failed: {exc}")

        return ToolOutput(success=True, data={"mode": "running", "running": running})
