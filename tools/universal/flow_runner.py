"""Safe sequential execution of skill-based declarative flows."""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agents.models import AgentPayload
from approval import ApprovalDecision
from approval.interaction import UserInteraction
from flows.models import flow_fingerprint
from flows.registry import FlowRegistry
from flows.store import FlowRun, FlowRunStore
from flows.validation import (
    resolve_execution_tools,
    schema_errors,
    validate_flow_capabilities,
)
from inference.exceptions import missing_resource
from utils.handoff import declared_artifact_paths, missing_artifact_paths
from utils.tasks import spawn
from utils.time import localnow

logger = logging.getLogger(__name__)

# A run stopped at a step: ``paused`` needs you (an error to look at), ``waiting``
# needs a resource and resumes by itself.
STOPPED = frozenset({"paused", "waiting"})
# How long a waiting run waits before it is tried again. Cooldowns usually end within a minute.
WAITING_RETRY_SECONDS = 60.0


def _status_for(exc: BaseException) -> str:
    return "waiting" if missing_resource(exc) else "paused"


def _waiting_note(exc: BaseException) -> str:
    resource = missing_resource(exc)
    return f"Waiting for {resource}: {exc}" if resource else ""


class FlowRunner:
    """Run each flow step as an agent executing one required skill.

    Flows coordinate procedures, not atomic operations. The selected agent gets
    the exact skill body as its execution contract and calls ordinary North tools
    through its existing tool loop. Those calls remain visible in the ledger and
    are guarded by the step's server-owned mutation policy.

    A step that errors pauses the run at that step, with the error and every
    earlier step's output (CODING_STYLE §13.5, #33). Running the same run id
    again resumes it there: finished steps are not repeated, and the approval
    layer refuses any action that already reached someone or spent money in
    this run. Only a decision of yours ends a run early (``rejected``).

    A step stopped by a missing resource - no model, no network - is not an
    error to look at: the run ``waiting`` resumes by itself once the resource
    is back (`resume_waiting`, #34).
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
        self._actions: dict[str, Callable[[], Awaitable[str]]] = {}
        self._flows = flow_registry
        self._agents = agent_registry
        self._skills = skill_registry
        self._tools = tool_registry
        self._store = store
        self._interaction = interaction
        self._workspace = workspace
        self._resuming: set[str] = set()

    def resume_waiting(self, min_wait_seconds: float = WAITING_RETRY_SECONDS) -> list[str]:
        """Start again every run waiting on a resource that has waited long enough; return their ids.

        A cooldown usually ends within a minute, so a waiting run is tried again
        once it has waited ``min_wait_seconds``. Trying costs nothing while the
        resource is still missing: the model call fails before any tokens are
        spent, and the run goes back to waiting. One run per flow at a time, in
        the background, and never one that is already being resumed.
        """
        now = datetime.now(UTC)
        started: list[str] = []
        seen_flows: set[str] = set()
        for run in self._store.list_runs(limit=200):
            if run.status != "waiting" or run.run_id in self._resuming or run.flow_name in seen_flows:
                continue
            seen_flows.add(run.flow_name)
            if (now - datetime.fromisoformat(run.updated_at)).total_seconds() < min_wait_seconds:
                continue
            self._resuming.add(run.run_id)
            spawn(self._resume(run), name=f"flow_resume:{run.run_id}")
            started.append(run.run_id)
        return started

    def waiting_run(self, flow_name: str) -> FlowRun | None:
        """The run of *flow_name* waiting on a resource, if any."""
        return self._store.waiting_run(flow_name)

    async def _resume(self, run: FlowRun) -> None:
        try:
            await self.run(
                run.flow_name,
                run_id=run.run_id,
                task_id=run.task_id,
                test_mode=run.test_mode,
                trigger=run.trigger,
            )
        except Exception as exc:
            # The flow changed or was retired while the run waited: say so, and
            # leave it for you rather than retrying something that cannot run.
            current = self._store.get(run.run_id)
            if current is not None and current.status in STOPPED | {"running"}:
                self._store.update(
                    run.run_id,
                    status="paused",
                    current_step=current.current_step,
                    outputs=current.outputs,
                    error=f"Could not resume: {exc}",
                )
        finally:
            self._resuming.discard(run.run_id)

    def register_action(self, name: str, handler: Callable[[], Awaitable[str]]) -> None:
        """Make a maintenance job runnable as a built-in flow's step.

        The handler is deterministic server code that returns a one-line account
        of what it did. It is registered by whoever owns the state it touches,
        after that state exists, so the runner is built without it.
        """
        self._actions[name] = handler

    async def run(
        self,
        flow_name: str,
        *,
        run_id: str | None = None,
        task_id: str = "",
        agent: str = "general",
        inputs: dict[str, Any] | None = None,
        test_mode: bool = False,
        trigger: str = "",
    ) -> FlowRun:
        run = await self._execute(
            flow_name,
            run_id=run_id,
            task_id=task_id,
            agent=agent,
            inputs=inputs,
            test_mode=test_mode,
            trigger=trigger,
        )
        if run.trigger == "schedule" and not run.test_mode:
            await self._deliver(run)
        return run

    async def _deliver(self, run: FlowRun) -> None:
        """Tell the user how a scheduled run ended: what it produced, or why it stopped.

        A schedule runs while nobody is watching, so its result has to be brought to the user. Scheduled
        briefings were written to a file every morning and never sent anywhere. A run that is waiting
        resumes by itself and says nothing yet.
        """
        if self._interaction is None or run.status not in _ENDED:
            return
        if run.status == "completed":
            title, message = f"{run.flow_name} is ready", _produced(run)
        else:
            title = f"{run.flow_name} stopped"
            message = f"{run.error or 'It did not finish.'}\n\nResume it on the Flows page."
        try:
            await self._interaction.inform(task_id=run.task_id or None, agent="flow", title=title, message=message)
        except Exception:
            logger.warning("Could not tell the user how scheduled flow %s ended", run.flow_name, exc_info=True)

    async def _execute(
        self,
        flow_name: str,
        *,
        run_id: str | None = None,
        task_id: str = "",
        agent: str = "general",
        inputs: dict[str, Any] | None = None,
        test_mode: bool = False,
        trigger: str = "",
    ) -> FlowRun:
        flow = self._flows.get(flow_name)
        if flow.status != "active" and not (test_mode and flow.status == "candidate"):
            raise ValueError(f"Flow '{flow_name}' is {flow.status}, not active")

        report = validate_flow_capabilities(
            flow,
            skill_registry=self._skills,
            agent_registry=self._agents,
            tool_registry=self._tools,
            allow_candidate_skills=test_mode,
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
                trigger=trigger or ("test" if test_mode else "manual"),
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
        # Only the step a paused or waiting run stopped on is told why it runs again.
        resumed_error = run.error if run.status in STOPPED else ""
        resume_at = run.current_step
        for index in range(run.current_step, len(flow.steps)):
            step = flow.steps[index]
            if step.action:
                handler = self._actions.get(step.action)
                if handler is None:
                    return self._store.update(
                        run.run_id,
                        status="paused",
                        current_step=index,
                        outputs=outputs,
                        error=f"Step '{step.name}': system action '{step.action}' is not available.",
                    )
                try:
                    summary = await handler()
                except Exception as exc:
                    return self._store.update(
                        run.run_id,
                        status=_status_for(exc),
                        current_step=index,
                        outputs=outputs,
                        error=_waiting_note(exc) or f"Step '{step.name}' failed: {exc}",
                    )
                outputs.append(
                    {
                        "step": step.name,
                        "skill": "",
                        "agent": "system",
                        "data": {
                            "output": summary,
                            "summary": summary,
                            "result": {},
                            "tools_used": [],
                            "agent_run_id": "",
                            "artifacts": [],
                        },
                    }
                )
                run = self._store.update(run.run_id, status="running", current_step=index + 1, outputs=outputs)
                continue
            skill = self._skills.get(step.skill) if step.skill else None
            execution = skill.execution if skill else None
            try:
                step_inputs = _resolve_inputs(step.inputs, run.inputs, outputs)
                input_errors = schema_errors(step_inputs, execution.inputs, path="inputs") if execution else []
                if input_errors:
                    raise ValueError("; ".join(input_errors))
            except ValueError as exc:
                return self._store.update(
                    run.run_id,
                    status="paused",
                    current_step=index,
                    outputs=outputs,
                    error=f"Step '{step.name}' needs valid inputs: {exc}",
                )
            selected_agent_name = execution.agent if execution else "general"
            allowed_tools = resolve_execution_tools(execution, step.inputs) if execution else ()
            # The step's own approval, never the skill's declared baseline
            # (execution.approval): a flow step must be able to require
            # different approval than the skill's default for this one use,
            # which is the entire reason FlowStep carries its own field.
            # Reading execution.approval here silently dropped every step's
            # override - a step declaring "before_step" ran with whatever the
            # skill's baseline happened to be instead.
            approval = step.approval
            success_criteria = (
                execution.success_criteria if execution else ("The step completed and returned useful evidence.",)
            )
            output_schema = execution.outputs if execution else {"type": "object", "properties": {}}

            if approval == "before_step":
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
                        f"Step '{step.name}' is ready to run using {step.skill or 'inline instructions'} "
                        f"with executor '{selected_agent_name}'.\n\n{step.instructions}"
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

            step_task_id = run.task_id or f"flow:{run.run_id}"
            try:
                selected_agent = self._agents.get(selected_agent_name)
                result = await selected_agent.run(
                    AgentPayload(
                        allow_candidate_skills=test_mode,
                        task_id=step_task_id,
                        prompt=_step_prompt(
                            flow.name,
                            step,
                            step_inputs,
                            outputs,
                            success_criteria,
                            output_schema,
                            resumed_error=resumed_error if index == resume_at else "",
                        ),
                        workspace=self._workspace,
                        # Configured by the operator (settings), so the server grants it.
                        granted_workspace=self._workspace or "",
                        skills=[step.skill] if step.skill else [],
                        allowed_tools=list(allowed_tools),
                        # "on_mutation" needs nothing here: every change a step
                        # makes already goes to the approval layer.
                        mutation_policy="deny" if approval == "never" else "allow",
                        allow_delegation=False,
                    )
                )
            except Exception as exc:
                return self._store.update(
                    run.run_id,
                    status=_status_for(exc),
                    current_step=index,
                    outputs=outputs,
                    error=_waiting_note(exc) or f"Skill step '{step.name}' failed: {exc}",
                )

            if result.requires_approval or result.has_question:
                return self._store.update(
                    run.run_id,
                    status="paused",
                    current_step=index,
                    outputs=outputs,
                    error=result.question or f"Skill step '{step.name}' needs user attention.",
                )

            # What the agent promises to leave behind (its config's `produces`) is
            # the durable result a schedule exists for. A model saying it wrote the
            # file is not the file: unattended runs fail when it is absent, so a
            # briefing that was never saved is not recorded as a success.
            declared = declared_artifact_paths(
                getattr(selected_agent.config, "produces", None) or [],
                step_task_id,
                localnow().date().isoformat(),
            )
            missing = missing_artifact_paths(declared)
            if missing and run.trigger == "schedule":
                return self._store.update(
                    run.run_id,
                    status="paused",
                    current_step=index,
                    outputs=outputs,
                    error=f"Skill step '{step.name}': {selected_agent_name} completed without writing: "
                    + ", ".join(missing),
                )
            written = [path for path in declared if path not in missing]

            contract_output = _contract_output(result, output_schema)
            output_errors = schema_errors(contract_output, output_schema, path="output")
            if output_errors:
                return self._store.update(
                    run.run_id,
                    status="paused",
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
                        "artifacts": written,
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


_ENDED = frozenset({"completed", "failed", "paused", "rejected"})
_MAX_DELIVERED_CHARS = 3_500  # Telegram takes 4,096 per message, with the card's own lines around it


def _produced(run: FlowRun) -> str:
    """What a finished run made, for the user: the last file it wrote, else its last step's summary."""
    files: list[str] = []
    for item in run.outputs:
        data = item.get("data") or {}
        files.extend(str(path) for path in data.get("artifacts") or [])
        named = (data.get("result") or {}).get("file")
        if isinstance(named, str) and named:
            files.append(named)
    for path in reversed(files):
        try:
            text = Path(path).expanduser().read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            continue
        if text:
            if len(text) > _MAX_DELIVERED_CHARS:
                text = text[:_MAX_DELIVERED_CHARS].rstrip() + "\n\n[cut short]"
            return f"{text}\n\n_Saved at {path}_"
    last = (run.outputs[-1].get("data") or {}) if run.outputs else {}
    summary = str(last.get("summary") or last.get("output") or "").strip()
    if summary and summary not in ("{}", "[]"):
        return summary
    return "Finished. The Flows page shows what it did."


def _step_prompt(
    flow_name: str,
    step,
    inputs: dict[str, Any],
    outputs: list[dict[str, Any]],
    success_criteria: tuple[str, ...],
    output_schema: dict[str, Any],
    *,
    resumed_error: str = "",
) -> str:
    previous = {item["step"]: item.get("data", {}) for item in outputs}
    criteria = "\n".join(f"- {item}" for item in success_criteria)
    resumed = (
        f"This step is running again after it stopped with: {resumed_error}\n"
        "Check what it already did before acting. Anything that reached someone else or spent money "
        "earlier in this run is refused if you try it again, so do not retry those.\n\n"
        if resumed_error
        else ""
    )
    return (
        f"Execute flow '{flow_name}', step '{step.name}', using {step.skill or 'the inline procedure'}.\n\n"
        f"{resumed}"
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
                    raise ValueError(f"Missing input reference {value}")
                current = current[part]
            return current
        if isinstance(value, dict):
            return {key: resolve(item) for key, item in value.items()}
        if isinstance(value, list):
            return [resolve(item) for item in value]
        return value

    return {key: resolve(value) for key, value in inputs.items()}
