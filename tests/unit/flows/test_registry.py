"""Tests for declarative flow discovery and validation."""

from __future__ import annotations

from pathlib import Path

from flows.exceptions import FlowNotFoundError
from flows.models import FlowSource
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
