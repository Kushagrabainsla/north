"""Executable validation for declarative, skill-based flows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from flows.models import flow_fingerprint
from utils.retired_tools import RETIRED_TOOLS, current_tool
from utils.tools import named_tools

_APPROVAL_RANK = {"never": 0, "on_mutation": 1, "before_step": 2}

# Tools every flow step has, whatever its skill lists. North cuts a long tool result
# and tells the agent to call read_tool_output for the rest; a step without it could
# not, and the research briefing failed that call in all 4 runs (issue #82, item 4).
# It only reads what the step's own tools returned.
STEP_TOOLS: tuple[str, ...] = ("read_tool_output",)


def step_tools(execution, inputs: dict[str, Any]) -> tuple[str, ...]:
    """The tools one flow step may use: its skill's list, then the tools every step has."""
    listed = resolve_execution_tools(execution, inputs)
    return listed + tuple(name for name in STEP_TOOLS if name not in listed)


@dataclass(frozen=True)
class FlowValidationReport:
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict[str, Any]:
        return {"valid": self.valid, "errors": list(self.errors), "warnings": list(self.warnings)}


def resolve_execution_tools(execution, inputs: dict[str, Any]) -> tuple[str, ...]:
    """Resolve a skill's exact tool allowlist for one flow step.

    ``$input.<name>`` is supported only for the legacy compatibility skill. It
    must resolve to a static tool name in the flow definition, never a runtime
    value, so validation can still prove the allowlist before execution. A
    retired tool's name resolves to the tool that replaced it
    (`utils/retired_tools.py`), so a skill saved before the change still runs.
    """
    resolved: list[str] = []
    for declared in execution.tools:
        name = declared
        if declared.startswith("$input."):
            value: Any = inputs
            for part in declared.removeprefix("$input.").split("."):
                if not isinstance(value, dict) or part not in value:
                    raise ValueError(f"tool reference {declared!r} does not resolve from step inputs")
                value = value[part]
            if not isinstance(value, str) or not value.strip() or value.startswith("${"):
                raise ValueError(f"tool reference {declared!r} must resolve to a static tool name")
            name = value.strip()
        name = current_tool(name)
        if name not in resolved:
            resolved.append(name)
    return tuple(resolved)


def schema_errors(
    value: Any,
    schema: dict[str, Any],
    *,
    path: str = "value",
    allow_references: bool = False,
) -> list[str]:
    """Validate the intentionally small JSON-schema subset in skill contracts."""
    if allow_references and isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        return []
    expected = schema.get("type")
    checks = {
        "object": lambda item: isinstance(item, dict),
        "array": lambda item: isinstance(item, list),
        "string": lambda item: isinstance(item, str),
        "boolean": lambda item: isinstance(item, bool),
        "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
        "number": lambda item: isinstance(item, (int, float)) and not isinstance(item, bool),
        "null": lambda item: item is None,
    }
    if expected in checks and not checks[expected](value):
        return [f"{path} must be {expected}"]
    if "enum" in schema and value not in schema["enum"]:
        return [f"{path} must be one of {schema['enum']}"]

    errors: list[str] = []
    if expected == "object" and isinstance(value, dict):
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        for name in required:
            if name not in value:
                errors.append(f"{path}.{name} is required")
        if schema.get("additionalProperties") is False:
            for name in value:
                if name not in properties:
                    errors.append(f"{path}.{name} is not declared")
        for name, child in properties.items():
            if name in value and isinstance(child, dict):
                errors.extend(
                    schema_errors(
                        value[name],
                        child,
                        path=f"{path}.{name}",
                        allow_references=allow_references,
                    )
                )
    elif expected == "array" and isinstance(value, list) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(value):
            errors.extend(
                schema_errors(
                    item,
                    schema["items"],
                    path=f"{path}[{index}]",
                    allow_references=allow_references,
                )
            )
    return errors


def validate_flow_capabilities(
    flow,
    *,
    skill_registry=None,
    agent_registry=None,
    tool_registry=None,
    allow_candidate_skills: bool = False,
) -> FlowValidationReport:
    """Validate skill-backed and inline-instruction flow steps."""
    errors: list[str] = []
    warnings: list[str] = []

    for index, step in enumerate(flow.steps, start=1):
        prefix = f"step {index} ({step.name!r})"
        if step.action:
            # A system action is server code named by a built-in flow: there is
            # no skill or instruction text to check. Whether the server has it is
            # answered when the step runs.
            continue
        if not step.skill:
            if not step.instructions.strip():
                errors.append(f"{prefix}: inline instructions are required when no skill is selected")
            continue
        if skill_registry is None:
            warnings.append(f"{prefix}: skill {step.skill!r} could not be checked")
            continue
        try:
            skill = skill_registry.get(step.skill)
        except Exception:
            errors.append(f"{prefix}: unknown skill {step.skill!r}")
            continue
        candidate_allowed = allow_candidate_skills and skill.status == "candidate"
        if skill.status != "active" and not candidate_allowed:
            errors.append(f"{prefix}: skill {step.skill!r} is {skill.status}, not active")
        elif candidate_allowed:
            warnings.append(f"{prefix}: candidate skill {step.skill!r} is available for test runs only")
        execution = skill.execution
        if execution is None:
            # Advisory skills are valid instruction sources. They run on
            # north with the same safe mutation gate as inline
            # instructions, but cannot declare tool or I/O contracts.
            continue

        if _APPROVAL_RANK[step.approval] < _APPROVAL_RANK[execution.approval]:
            # The skill author's declared approval is a floor, not a default a
            # step can silently relax: a skill that says "ask before mutating"
            # must not become "never ask" just because a flow step said so.
            errors.append(
                f"{prefix}: approval {step.approval!r} is below skill {step.skill!r}'s minimum {execution.approval!r}"
            )

        errors.extend(
            f"{prefix}: {message}"
            for message in schema_errors(
                step.inputs,
                execution.inputs,
                path="inputs",
                allow_references=True,
            )
        )

        # North runs every step; the skill's `execution.agent` no longer chooses one.
        if agent_registry is None:
            warnings.append(f"{prefix}: whether north may use skill {step.skill!r} could not be checked")
        else:
            north = agent_registry.north
            if not skill.available_to(north.domain) and not (candidate_allowed and north.domain in skill.domains):
                serves = sorted(skill.domains)
                errors.append(f"{prefix}: skill {step.skill!r} is not one north uses (it serves {serves})")

        try:
            tool_names = resolve_execution_tools(execution, step.inputs)
        except ValueError as exc:
            errors.append(f"{prefix}: {exc}")
            continue
        if tool_registry is None:
            if tool_names:
                warnings.append(f"{prefix}: allowed tools could not be checked")
        else:
            for tool_name in tool_names:
                try:
                    tool_registry.get(tool_name)
                except Exception:
                    errors.append(f"{prefix}: unknown tool {tool_name!r} in skill contract")
            errors.extend(f"{prefix}: {message}" for message in _unlisted_tools(step, skill, tool_names, tool_registry))

    return FlowValidationReport(tuple(errors), tuple(warnings))


def _unlisted_tools(step, skill, tool_names: tuple[str, ...], tool_registry) -> list[str]:
    """A tool the step's text tells the agent to call, which the step cannot use.

    A step can use only the tools its skill lists. The news briefing called fetch_url
    in every run and each call failed; the outreach step was told to call
    search_files, which it did not have (issue #82, item 4). A retired name counts as
    the tool that replaced it, as it does when the step runs.
    """
    try:
        known = set(tool_registry.all_tool_names())
    except Exception:
        return []
    text = f"{skill.description}\n{skill.body}\n{step.instructions}"
    usable = set(tool_names) | set(STEP_TOOLS)
    messages = []
    for name in sorted(named_tools(text, known | set(RETIRED_TOOLS))):
        tool = current_tool(name)
        if tool in usable:
            continue
        shown = name if tool == name else f"{name} (now {tool})"
        messages.append(
            f"its text names tool {shown!r}, which skill {step.skill!r} does not list; "
            f"add {tool!r} to the skill's tools or take it out of the text"
        )
    return messages


def schedulable_flow_error(
    flow,
    *,
    skill_registry=None,
    agent_registry=None,
    tool_registry=None,
) -> str:
    """Why *flow* cannot be put on a schedule right now, or "" when it can.

    A schedule promises a run nobody is watching, so the flow has to be active,
    unchanged since it was tested, and still executable. Every door that creates
    or retargets a schedule asks this one question.
    """
    if flow.status != "active":
        return f"Flow '{flow.name}' is {flow.status}, not active."
    if (
        flow.activation_fingerprint
        and skill_registry is not None
        and flow.activation_fingerprint != flow_fingerprint(flow, skill_registry.get)
    ):
        return f"Flow '{flow.name}' changed after activation; test and activate it again."
    report = validate_flow_capabilities(
        flow,
        skill_registry=skill_registry,
        agent_registry=agent_registry,
        tool_registry=tool_registry,
    )
    if not report.valid:
        return f"Flow '{flow.name}' is no longer executable: {'; '.join(report.errors)}"
    return ""
