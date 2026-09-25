"""Tests for declarative flow discovery and validation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from flows.exceptions import FlowNotFoundError
from flows.models import Flow, FlowSource, FlowStep, flow_fingerprint
from flows.registry import FlowRegistry


def _write_flow(base: Path, name: str, text: str) -> None:
    directory = base / name
    directory.mkdir(parents=True)
    (directory / "FLOW.yaml").write_text(text, encoding="utf-8")


def test_loads_valid_flow(tmp_path):
    _write_flow(
        tmp_path,
        "review",
        """name: review
description: Review a prepared item
steps:
  - name: inspect
    skill: browser-research
    instructions: Inspect the prepared item.
    approval: never
  - name: approve
    skill: review-guidelines
    instructions: Review the inspection result.
    approval: always
""",
    )
    registry = FlowRegistry(tmp_path)
    flow = registry.get("review")
    assert registry.names() == ["review"]
    assert flow.source is FlowSource.BUILTIN
    assert flow.step_names() == ["inspect", "approve"]
    assert flow.steps[1].skill == "review-guidelines"
    assert flow.status == "active"


def test_legacy_tool_step_is_migrated_to_a_skill_invocation_in_memory(tmp_path):
    _write_flow(
        tmp_path,
        "legacy",
        """name: legacy
description: Preserve an older definition
steps:
  - name: inspect
    tool: browser
    params: {action: inspect}
    approval: never
""",
    )

    flow = FlowRegistry(tmp_path).get("legacy")
    step = flow.steps[0]

    assert step.skill == "using-a-north-tool"
    assert step.inputs == {"tool": "browser", "arguments": {"action": "inspect"}}
    assert "browser" in step.instructions
    assert flow.status == "candidate"


def test_legacy_boolean_approval_migrates_to_a_safe_candidate(tmp_path):
    _write_flow(
        tmp_path,
        "legacy",
        """name: legacy
description: Preserve an older definition
steps:
  - name: inspect
    tool: browser
    skill: browser-research-and-extraction
    approval: false
    description: Inspect notifications without submitting anything.
""",
    )

    flow = FlowRegistry(tmp_path).get("legacy")

    assert flow.status == "candidate"
    assert flow.steps[0].approval == "on_mutation"
    assert flow.steps[0].skill == "using-a-north-tool"


def test_legacy_agent_choice_is_removed_and_requires_retesting(tmp_path):
    _write_flow(
        tmp_path,
        "legacy-agent",
        """name: legacy-agent
description: Preserve an older executor choice
status: active
steps:
  - name: inspect
    skill: browser-research-and-extraction
    agent: general
    instructions: Inspect safely.
    approval: always
""",
    )

    flow = FlowRegistry(tmp_path).get("legacy-agent")

    assert flow.status == "candidate"
    assert not hasattr(flow.steps[0], "agent")


def test_skill_step_with_no_instructions_loads(tmp_path):
    """A step naming a skill needs no instructions of its own - the skill's body is
    the procedure. flows/validation.py already enforces this same
    rule at the semantic layer; the parser used to reject the step before
    validation ever ran, silently dropping any flow like this one - including
    what the dashboard's flow editor saves for a step whose kind is "existing
    skill" (see web/src/pages/Verbose.tsx FlowStepCard).
    """
    _write_flow(
        tmp_path,
        "skill-only",
        """name: skill-only
description: Run a skill with no inline instructions
steps:
  - name: run-it
    skill: browser-research
    instructions: ''
    approval: on_mutation
""",
    )
    registry = FlowRegistry(tmp_path)
    flow = registry.get("skill-only")
    assert flow.status == "active"
    assert flow.steps[0].skill == "browser-research"
    assert flow.steps[0].instructions == ""


def test_step_with_neither_skill_nor_instructions_is_rejected(tmp_path):
    _write_flow(
        tmp_path,
        "empty-step",
        """name: empty-step
description: Missing both a skill and instructions
steps:
  - name: run-it
    approval: on_mutation
""",
    )
    assert FlowRegistry(tmp_path).names() == []


def test_skips_invalid_flows_without_crashing(tmp_path):
    _write_flow(tmp_path, "missing-description", "name: missing-description\nsteps: []\n")
    _write_flow(
        tmp_path,
        "good",
        "name: good\ndescription: A good flow\nsteps:\n  - name: one\n    tool: browser\n",
    )
    assert FlowRegistry(tmp_path).names() == ["good"]


def test_rejects_duplicate_steps_and_unknown_approval(tmp_path):
    _write_flow(
        tmp_path,
        "bad",
        """name: bad
description: Bad flow
steps:
  - name: same
    tool: browser
  - name: same
    tool: browser
""",
    )
    _write_flow(
        tmp_path,
        "also-bad",
        """name: also-bad
description: Bad approval
steps:
  - name: one
    tool: browser
    approval: maybe
""",
    )
    assert FlowRegistry(tmp_path).names() == []


def test_learned_flow_override_takes_precedence_over_builtin(tmp_path):
    builtin, learned = tmp_path / "builtin", tmp_path / "learned"
    _write_flow(builtin, "review", "name: review\ndescription: Built in\nsteps:\n  - name: one\n    tool: browser\n")
    _write_flow(learned, "review", "name: review\ndescription: Learned\nsteps:\n  - name: one\n    tool: browser\n")
    registry = FlowRegistry(builtin, learned)
    assert registry.get("review").description == "Learned"
    assert registry.get("review").source is FlowSource.LEARNED


def test_get_unknown_flow_raises(tmp_path):
    try:
        FlowRegistry(tmp_path).get("missing")
    except FlowNotFoundError:
        pass
    else:
        raise AssertionError("expected FlowNotFoundError")


def test_a_built_in_flow_may_run_a_system_action_and_it_needs_no_skill_or_instructions(tmp_path):
    _write_flow(
        tmp_path,
        "cleanup",
        """name: cleanup
description: Housekeeping
steps:
  - name: clean-up
    action: task_context_cleanup
""",
    )

    step = FlowRegistry(tmp_path).get("cleanup").steps[0]

    assert step.action == "task_context_cleanup"
    assert not step.skill and not step.instructions


def test_a_users_flow_cannot_run_a_system_action(tmp_path):
    builtin, learned = tmp_path / "builtin", tmp_path / "learned"
    builtin.mkdir()
    _write_flow(
        learned,
        "sneaky",
        """name: sneaky
description: Tries to borrow north's own maintenance
steps:
  - name: clean-up
    action: task_context_cleanup
""",
    )

    assert FlowRegistry(builtin, learned).names() == []


def test_a_flow_without_a_system_action_keeps_the_fingerprint_it_was_activated_under():
    plain = Flow(
        name="f",
        description="d",
        steps=(FlowStep(name="s", skill="", instructions="do it"),),
        directory=Path("."),
    )
    with_action = Flow(
        name="f",
        description="d",
        steps=(FlowStep(name="s", skill="", instructions="do it", action="clean"),),
        directory=Path("."),
    )
    # The definition as it was hashed before steps could name an action. A flow
    # activated then must still match, or every active flow would need re-testing.
    before = {
        "name": "f",
        "description": "d",
        "domains": ["general"],
        "steps": [
            {"name": "s", "skill": "", "instructions": "do it", "inputs": {}, "approval": "on_mutation"}
        ],
        "skills": {},
    }
    expected = hashlib.sha256(json.dumps(before, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    assert flow_fingerprint(plain) == expected
    assert flow_fingerprint(with_action) != expected
