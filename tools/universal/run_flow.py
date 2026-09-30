"""RunFlowTool - start or resume a declarative flow."""

from __future__ import annotations

from typing import Any

from tools.base import Tool
from tools.models import ToolInput, ToolOutput
from tools.universal.flow_runner import FlowRunner

_NEEDS_YOU = frozenset({"failed", "needs_approval", "rejected", "paused"})
_ALREADY_WAITING = "An earlier run of this flow is waiting for a model or the network; it runs, late, when one returns."


class RunFlowTool(Tool):
    name = "run_flow"
    is_mutating = True
    description = (
        "Start or resume a named skill-based flow. The flow runs sequentially and persists checkpoints; "
        "a step that errors pauses the run there, and passing its run_id resumes it at that step. "
        "Test mode is evidence collection, not a dry run: "
        "skills may execute real tools and keep the same approval boundaries, so use the smallest safe case."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Flow name."},
            "run_id": {"type": "string", "description": "Existing run ID to resume, if any."},
            "task_id": {"type": "string", "description": "Optional North task ID for approval events."},
            "inputs": {"type": "object", "description": "Initial values available as ${inputs.key}."},
            "mode": {
                "type": "string",
                "enum": ["execute", "test"],
                "description": "Use test to execute a candidate on the smallest safe case before activation.",
                "default": "execute",
            },
        },
        "required": ["name"],
    }

    def __init__(self, runner: FlowRunner) -> None:
        self._runner = runner

    async def run(self, input: ToolInput) -> ToolOutput:
        name = str(input.params.get("name") or "").strip()
        if not name:
            return ToolOutput(success=False, error="Parameter 'name' is required.")
        mode = str(input.params.get("mode") or "execute").strip().lower()
        if mode not in {"execute", "test"}:
            return ToolOutput(success=False, error="Parameter 'mode' must be execute or test.")
        return await self._start(
            name,
            run_id=str(input.params.get("run_id") or "").strip() or None,
            task_id=str(input.params.get("task_id") or "").strip(),
            inputs=input.params.get("inputs") if isinstance(input.params.get("inputs"), dict) else None,
            test_mode=mode == "test",
        )

    async def run_scheduled(self, name: str, job_id: str) -> ToolOutput:
        """Run *name* because a schedule fired.

        Separate from ``run`` so a run is recorded as scheduled only when the
        scheduler started it: the trigger is not a parameter an agent could
        set on itself.

        While an earlier run of the flow waits on a missing resource, no second
        one starts (#34): it would only wait beside it. The waiting run runs,
        late, when the resource returns.
        """
        waiting = self._runner.waiting_run(name)
        if waiting is not None:
            return ToolOutput(success=True, data={**_view(waiting), "note": _ALREADY_WAITING})
        return await self._start(name, task_id=job_id, trigger="schedule")

    async def _start(
        self,
        name: str,
        *,
        run_id: str | None = None,
        task_id: str = "",
        inputs: dict[str, Any] | None = None,
        test_mode: bool = False,
        trigger: str = "",
    ) -> ToolOutput:
        try:
            run = await self._runner.run(
                name,
                run_id=run_id,
                task_id=task_id,
                inputs=inputs,
                test_mode=test_mode,
                trigger=trigger,
            )
            # A paused run stopped on something only you can resolve - an error
            # to look at, a card - so it is not a success. It waits, resumable.
            return ToolOutput(success=run.status not in _NEEDS_YOU, data=_view(run))
        except Exception as exc:
            return ToolOutput(success=False, error=str(exc))

    def format_output(self, data: dict[str, Any]) -> str:
        return (
            f"Flow {data.get('run_id')} is {data.get('status')} "
            f"at step {data.get('current_step')}; {len(data.get('outputs') or [])} step(s) completed."
        )


def _view(run) -> dict[str, Any]:
    return {
        "run_id": run.run_id,
        "flow_name": run.flow_name,
        "status": run.status,
        "current_step": run.current_step,
        "outputs": run.outputs,
        "error": run.error,
        "test_mode": run.test_mode,
    }
