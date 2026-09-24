"""Safe sequential execution of skill-based declarative flows."""

from __future__ import annotations

import json
import uuid
from typing import Any

from agents.models import AgentPayload
from approval import ApprovalDecision
from approval.interaction import UserInteraction
from flows.models import flow_fingerprint
from flows.registry import FlowRegistry
from flows.store import FlowRun, FlowRunStore
from tools.universal._flow_validation import (
    resolve_execution_tools,
    schema_errors,
    validate_flow_capabilities,
)


class FlowRunner:
    """Run each flow step as an agent executing one required skill.

    Flows coordinate procedures, not atomic operations. The selected agent gets
    the exact skill body as its execution contract and calls ordinary North tools
    through its existing tool loop. Those calls remain visible in the ledger and
    are guarded by the step's server-owned mutation policy.
    """

    def __init__(
        self,
        flow_registry: FlowRegistry,
        agent_registry,
        skill_registry,
        store: FlowRunStore,
        interaction: UserInteraction | None = None,
        *,
        tool_registry=None,
        workspace: str = "",
    ) -> None:
        self._flows = flow_registry
        self._agents = agent_registry
        self._skills = skill_registry
        self._tools = tool_registry
        self._store = store
        self._interaction = interaction
        self._workspace = workspace

    async def run(
        self,
        flow_name: str,
        *,
        run_id: str | None = None,
        task_id: str = "",
        agent: str = "general",
        inputs: dict[str, Any] | None = None,
        test_mode: bool = False,
    ) -> FlowRun:
        flow = self._flows.get(flow_name)
        if flow.status != "active" and not (test_mode and flow.status == "candidate"):
            raise ValueError(f"Flow '{flow_name}' is {flow.status}, not active")

        report = validate_flow_capabilities(
            flow,
            skill_registry=self._skills,
            agent_registry=self._agents,
            tool_registry=self._tools,
        )
        if not report.valid:
            raise ValueError("Flow is not executable: " + "; ".join(report.errors))

        fingerprint = flow_fingerprint(flow, self._skills.get)
        if (
            flow.status == "active"
            and not test_mode
            and flow.activation_fingerprint
            and flow.activation_fingerprint != fingerprint
        ):
            raise ValueError(
                f"Flow '{flow_name}' changed after activation because a referenced skill was edited; "
                "test and activate it again"
            )

        run_id = run_id or uuid.uuid4().hex
        run = self._store.get(run_id)
        if run is None:
            run = self._store.create(
                run_id=run_id,
                flow_name=flow.name,
                task_id=task_id,
                agent=agent,
                inputs=inputs,
                flow_fingerprint=fingerprint,
                test_mode=test_mode,
            )
        elif run.flow_name != flow.name:
            raise ValueError(f"Run '{run_id}' belongs to flow '{run.flow_name}'")
        elif run.flow_fingerprint and run.flow_fingerprint != fingerprint:
            raise ValueError(f"Flow '{flow_name}' or one of its skills changed after run '{run_id}' started")
        elif run.test_mode != test_mode:
            raise ValueError(f"Run '{run_id}' cannot switch between test and execute mode")
        elif run.status in {"completed", "cancelled", "rejected"}:
            return run

        outputs = list(run.outputs)
        for index in range(run.current_step, len(flow.steps)):
            step = flow.steps[index]
            step_inputs = _resolve_inputs(step.inputs, run.inputs, outputs)
            skill = self._skills.get(step.skill) if step.skill else None
            execution = skill.execution if skill else None
            selected_agent_name = execution.agent if execution else "general"
            allowed_tools = resolve_execution_tools(execution, step.inputs) if execution else ()
            approval = execution.approval if execution else "on_mutation"
            success_criteria = execution.success_criteria if execution else ("The step completed and returned useful evidence.",)
            output_schema = execution.outputs if execution else {"type": "object", "properties": {}}

            if approval == "always":
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
                    agent=selected_agent_name,
                    title=f"Flow approval: {flow.name}",
                    message=(
                        f"Step '{step.name}' is ready to run "
                        f"using {step.skill or 'inline instructions'} with executor '{selected_agent_name}'.\n\n{step.instructions}"
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

            try:
                selected_agent = self._agents.get(selected_agent_name)
                result = await selected_agent.run(
                    AgentPayload(
                        task_id=run.task_id or f"flow:{run.run_id}",
                        prompt=_step_prompt(
                            flow.name,
                            step,
                            step_inputs,
                        outputs,
                        success_criteria,
                        output_schema,
                    ),
                        workspace=self._workspace,
                        model_pool=selected_agent.config.model_pool or "reasoning",
                        skills=[step.skill] if step.skill else [],
                        allowed_tools=list(allowed_tools),
                        mutation_policy={
                            "never": "deny",
                            "on_mutation": "require_approval",
                            "always": "allow",
                        }[approval],
                        allow_delegation=False,
                    )
                )
            except Exception as exc:
                return self._store.update(
                    run.run_id,
                    status="failed",
                    current_step=index,
                    outputs=outputs,
                    error=f"Skill step '{step.name}' failed: {exc}",
                )

            if result.requires_approval or result.has_question:
                return self._store.update(
                    run.run_id,
                    status="paused",
                    current_step=index,
                    outputs=outputs,
                    error=result.question or f"Skill step '{step.name}' needs user attention.",
                )

            contract_output = _contract_output(result, output_schema)
            output_errors = schema_errors(contract_output, output_schema, path="output")
            if output_errors:
                return self._store.update(
                    run.run_id,
                    status="failed",
                    current_step=index,
                    outputs=outputs,
                    error=(
                        f"Skill step '{step.name}' returned data outside skill "
                        f"'{step.skill or 'inline instructions'}'s output contract: {'; '.join(output_errors)}"
                    ),
                )

            outputs.append(
                {
                    "step": step.name,
                    "skill": step.skill or "inline-instructions",
                    "agent": selected_agent_name,
                    "data": {
                        "output": result.output,
                        "summary": result.summary,
                        "result": contract_output,
                        "tools_used": result.tools_used,
                        "agent_run_id": result.run_id,
                    },
                }
            )
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


def _step_prompt(
    flow_name: str,
    step,
    inputs: dict[str, Any],
    outputs: list[dict[str, Any]],
    success_criteria: tuple[str, ...],
    output_schema: dict[str, Any],
) -> str:
    previous = {item["step"]: item.get("data", {}) for item in outputs}
    criteria = "\n".join(f"- {item}" for item in success_criteria)
    return (
        f"Execute flow '{flow_name}', step '{step.name}', using {step.skill or 'the inline procedure'}.\n\n"
        f"Step instructions:\n{step.instructions}\n\n"
        f"Resolved inputs:\n{json.dumps(inputs, indent=2, default=str)}\n\n"
        f"Previous step outputs:\n{json.dumps(previous, indent=2, default=str)}\n\n"
        f"Required success criteria:\n{criteria}\n\n"
        "Complete only this step. Your final response must be one JSON object matching this output schema:\n"
        f"{json.dumps(output_schema, indent=2, default=str)}\n\n"
        "Do not wrap the JSON in commentary. Include enough evidence in that object for the next step to use."
    )


def _contract_output(result, schema: dict[str, Any]) -> dict[str, Any]:
    """Normalize an agent result into the object validated and handed forward."""
    data = result.data if isinstance(result.data, dict) else {}
    if not schema_errors(data, schema, path="output"):
        return data

    text = str(result.output or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return data
    return parsed if isinstance(parsed, dict) else data


def _resolve_inputs(
    inputs: dict[str, Any],
    flow_inputs: dict[str, Any],
    outputs: list[dict[str, Any]],
) -> dict[str, Any]:
    """Resolve the small, explicit reference syntax supported by skill inputs."""
    values = {"inputs": flow_inputs, "steps": {item["step"]: item.get("data", {}) for item in outputs}}

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

    return {key: resolve(value) for key, value in inputs.items()}
