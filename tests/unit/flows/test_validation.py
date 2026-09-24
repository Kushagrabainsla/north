"""Tests for server-enforced Flow -> Skill -> Tool contracts."""

from __future__ import annotations

from types import SimpleNamespace

from flows.models import Flow, FlowStep
from skills.models import Skill, SkillExecution
from tools.universal._flow_validation import validate_flow_capabilities


class _Registry:
    def __init__(self, values):
        self.values = values

    def get(self, name):
        if name not in self.values:
            raise KeyError(name)
        return self.values[name]


def _flow(tmp_path, *, inputs=None, approval="never", skill="strict"):
    return Flow(
        name="demo",
        description="Validate one step",
        directory=tmp_path,
        steps=(
            FlowStep(
                name="one",
                skill=skill,
                instructions="Execute the contract.",
                inputs=inputs or {},
                approval=approval,
            ),
        ),
    )


def _skill(tmp_path, *, execution=None):
    return Skill(
        name="strict",
        description="Use when validating a contract.",
        body="1. Validate it.",
        directory=tmp_path,
        domains=frozenset({"general"}),
        execution=execution,
    )


def test_validation_accepts_advisory_skill(tmp_path):
    """A skill with no execution contract (advisory: description and body
    only, no agent/tools/inputs) is a valid instruction source for a step -
    it runs through the general agent under the same safe mutation gate as
    inline instructions, just without a declared tool/IO contract to check."""
    report = validate_flow_capabilities(
        _flow(tmp_path),
        skill_registry=_Registry({"strict": _skill(tmp_path)}),
    )

    assert report.valid


def test_validation_enforces_inputs_approval_executor_and_tools(tmp_path):
    execution = SkillExecution(
        agent="general",
        tools=("browser",),
        approval="on_mutation",
        inputs={
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
            "additionalProperties": False,
        },
        outputs={"type": "object", "properties": {}},
        success_criteria=("The page was verified.",),
    )
    report = validate_flow_capabilities(
        _flow(tmp_path, inputs={"extra": True}, approval="never"),
        skill_registry=_Registry({"strict": _skill(tmp_path, execution=execution)}),
        agent_registry=_Registry({"general": SimpleNamespace(domain="general")}),
        tool_registry=_Registry({}),
    )

    assert not report.valid
    combined = "; ".join(report.errors)
    assert "minimum 'on_mutation'" in combined
    assert "inputs.url is required" in combined
    assert "inputs.extra is not declared" in combined
    assert "unknown tool 'browser'" in combined


def test_validation_resolves_legacy_dynamic_tool_without_wildcard(tmp_path):
    execution = SkillExecution(
        agent="general",
        tools=("$input.tool",),
        approval="on_mutation",
        inputs={
            "type": "object",
            "properties": {
                "tool": {"type": "string"},
                "arguments": {"type": "object"},
            },
            "required": ["tool", "arguments"],
        },
        outputs={"type": "object", "properties": {}},
        success_criteria=("The named tool completed.",),
    )
    report = validate_flow_capabilities(
        _flow(
            tmp_path,
            inputs={"tool": "browser", "arguments": {}},
            approval="on_mutation",
        ),
        skill_registry=_Registry({"strict": _skill(tmp_path, execution=execution)}),
        agent_registry=_Registry({"general": SimpleNamespace(domain="general")}),
        tool_registry=_Registry({"browser": object()}),
    )

    assert report.valid
