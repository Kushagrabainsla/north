"""System metrics tool — lets agents query performance data from the ledger."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from tools import Tool, ToolInput, ToolOutput

if TYPE_CHECKING:
    pass


class QueryMetricsTool(Tool):
    """Query system performance metrics from the ledger."""

    name = "query_metrics"
    description = (
        "Query aggregated system performance metrics from the ledger. "
        "Returns task counts, cost breakdown by agent and model, latency percentiles "
        "(p50/p95), token usage, and top error types over a configurable time window."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "days": {
                "type": "integer",
                "description": "Look-back window in days (default 7; max 365).",
                "default": 7,
            },
        },
        "required": [],
    }

    def __init__(self, ledger=None) -> None:
        self._ledger = ledger

    def format_output(self, data: dict[str, Any]) -> str:
        m = data.get("metrics", {})
        if not m:
            return "No metrics data available."

        lines = [f"## System Metrics — last {m.get('period_days', '?')} days\n"]
        lines.append(f"- **Tasks**: {m.get('total_tasks', 0)}")
        lines.append(f"- **Total cost**: ${m.get('total_cost_usd', 0):.6f}")
        lines.append(f"- **Tokens in**: {m.get('total_tokens_in', 0):,}")
        lines.append(f"- **Tokens out**: {m.get('total_tokens_out', 0):,}")

        by_agent = m.get("by_agent", [])
        if by_agent:
            lines.append("\n### By Agent")
            lines.append("| Agent | Tasks | Success | Cost | p50 ms | p95 ms |")
            lines.append("|-------|-------|---------|------|--------|--------|")
            for a in by_agent:
                p50 = str(a["p50_ms"]) if a.get("p50_ms") is not None else "—"
                p95 = str(a["p95_ms"]) if a.get("p95_ms") is not None else "—"
                lines.append(
                    f"| {a['agent']} | {a['tasks']} | "
                    f"{a['success_rate'] * 100:.0f}% | "
                    f"${a['cost_usd']:.6f} | {p50} | {p95} |"
                )

        by_model = m.get("by_model", {})
        if by_model:
            lines.append("\n### Cost by Model")
            for model, cost in by_model.items():
                lines.append(f"- `{model}`: ${cost:.6f}")

        top_errors = m.get("top_errors", {})
        if top_errors:
            lines.append("\n### Top Errors")
            for err, count in top_errors.items():
                lines.append(f"- `{err}`: {count}")

        return "\n".join(lines)

    async def run(self, input: ToolInput) -> ToolOutput:
        if self._ledger is None:
            return ToolOutput(success=False, error="Metrics tool not connected to ledger.")

        try:
            days = int(input.params.get("days", 7))
        except (ValueError, TypeError):
            days = 7
        days = max(1, min(days, 365))

        try:
            metrics = await self._ledger.get_metrics(days=days)
            return ToolOutput(success=True, data={"metrics": metrics})
        except Exception as exc:
            return ToolOutput(success=False, error=f"Failed to query metrics: {exc}")
