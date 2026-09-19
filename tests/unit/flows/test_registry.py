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
    tool: browser
    approval: never
  - name: approve
    tool: request_approval
    skill: review-guidelines
    approval: always
""",
    )
    registry = FlowRegistry(tmp_path)
    flow = registry.get("review")
    assert registry.names() == ["review"]
    assert flow.source is FlowSource.BUILTIN
    assert flow.step_names() == ["inspect", "approve"]
    assert flow.steps[1].skill == "review-guidelines"


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


def test_learned_flow_does_not_override_builtin(tmp_path):
    builtin, learned = tmp_path / "builtin", tmp_path / "learned"
    _write_flow(builtin, "review", "name: review\ndescription: Built in\nsteps:\n  - name: one\n    tool: browser\n")
    _write_flow(learned, "review", "name: review\ndescription: Learned\nsteps:\n  - name: one\n    tool: browser\n")
    registry = FlowRegistry(builtin, learned)
    assert registry.get("review").description == "Built in"
    assert registry.get("review").source is FlowSource.BUILTIN


def test_get_unknown_flow_raises(tmp_path):
    try:
        FlowRegistry(tmp_path).get("missing")
    except FlowNotFoundError:
        pass
    else:
        raise AssertionError("expected FlowNotFoundError")
