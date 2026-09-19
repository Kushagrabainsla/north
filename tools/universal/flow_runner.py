"""Safe sequential execution of declarative flows."""

from __future__ import annotations

import uuid
from typing import Any

from approval import ApprovalDecision
from approval.interaction import UserInteraction
from flows.registry import FlowRegistry
from flows.store import FlowRun, FlowRunStore
from tools.models import ToolInput
from tools.registry import ToolRegistry


class FlowRunner:
    """Run flow steps through the existing North tool registry.

    A runner never imports or executes flow-authored code. It resolves each
    declared tool through ``ToolRegistry`` and stops at an approval boundary.
    """

    def __init__(
        self,
        flow_registry: FlowRegistry,
        tool_registry: ToolRegistry,
        store: FlowRunStore,
        interaction: UserInteraction | None = None,
    ) -> None:
        self._flows = flow_registry
        self._tools = tool_registry
        self._store = store
        self._interaction = interaction

    async def run(
        self,
        flow_name: str,
        *,
        run_id: str | None = None,
        task_id: str = "",
        agent: str = "general",
        inputs: dict[str, Any] | None = None,
    ) -> FlowRun:
        flow = self._flows.get(flow_name)
        if flow.status != "active":
            raise ValueError(f"Flow '{flow_name}' is {flow.status}, not active")

        run_id = run_id or uuid.uuid4().hex
        run = self._store.get(run_id)
        if run is None:
            run = self._store.create(
                run_id=run_id,
                flow_name=flow.name,
                task_id=task_id,
                agent=agent,
                inputs=inputs,
            )
        elif run.flow_name != flow.name:
            raise ValueError(f"Run '{run_id}' belongs to flow '{run.flow_name}'")
        elif run.status in {"completed", "cancelled", "rejected"}:
            return run

        outputs = list(run.outputs)
        for index in range(run.current_step, len(flow.steps)):
            step = flow.steps[index]
            tool = self._tools.get(step.tool)
            if step.approval == "always" or (step.approval == "on_mutation" and tool.is_mutating):
                if self._interaction is None:
                    return self._store.update(
                        run.run_id,
                        status="paused",
                        current_step=index,
                        outputs=outputs,
                        error=f"Approval required before step '{step.name}'.",
                    )
                decision = await self._interaction.request_approval_status(
                    task_id=run.task_id or None,
                    agent=run.agent or "general",
                    title=f"Flow approval: {flow.name}",
                    message=(
                        f"Flow step '{step.name}' is ready to run using tool '{step.tool}'.\n\n"
                        f"{step.description or flow.description}"
                    ),
                )
                if decision is not ApprovalDecision.APPROVED:
                    return self._store.update(
                        run.run_id,
                        status="rejected",
                        current_step=index,
                        outputs=outputs,
                        error=f"Approval was not granted for step '{step.name}'.",
                    )
            if step.approval == "never" and tool.is_mutating:
                return self._store.update(
                    run.run_id,
                    status="needs_approval",
                    current_step=index,
                    outputs=outputs,
                    error=f"Mutating tool '{step.tool}' cannot run with approval='never'.",
                )

            params = _resolve_params(step.params, run.inputs, outputs)
            result = await tool.run(ToolInput(params=params))
            if not result.success:
                return self._store.update(
                    run.run_id,
                    status="failed",
                    current_step=index,
                    outputs=outputs,
                    error=result.error or f"Step '{step.name}' failed.",
                )
            outputs.append({"step": step.name, "tool": step.tool, "data": result.data})
            run = self._store.update(
                run.run_id,
                status="running",
                current_step=index + 1,
                outputs=outputs,
            )

        return self._store.update(
            run.run_id,
            status="completed",
            current_step=len(flow.steps),
            outputs=outputs,
        )


def _resolve_params(params: dict[str, Any], inputs: dict[str, Any], outputs: list[dict[str, Any]]) -> dict[str, Any]:
    """Resolve the small, explicit reference syntax supported by flow params."""
    values = {"inputs": inputs, "steps": {item["step"]: item.get("data", {}) for item in outputs}}

    def resolve(value: Any) -> Any:
        if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
            current: Any = values
            for part in value[2:-1].split("."):
                if not isinstance(current, dict) or part not in current:
                    return value
                current = current[part]
            return current
        if isinstance(value, dict):
            return {key: resolve(item) for key, item in value.items()}
        if isinstance(value, list):
            return [resolve(item) for item in value]
        return value

    return {key: resolve(value) for key, value in params.items()}
