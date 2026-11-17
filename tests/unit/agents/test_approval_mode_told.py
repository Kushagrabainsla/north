"""North tells the user how approvals work in the mode that is on (issue #82, item 2).

In Autonomous, north told the user a flow "sends nothing until you approve", and
then north approved the batch for them, as the mode is designed to do. The agent
was told only that "the approval mode decides who answers". Now its task message
names the mode and who answers a card in it.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from agents.models import AgentConfig, AgentDependencies, AgentPayload
from config.approval_mode import ApprovalMode
from memory import FileContextStore
from tests.conftest import MockInferenceRouter
from tools.confidence import ConfidenceTracker
from tools.registry import ToolRegistry

AGENTS_DIR = Path(__file__).parent.parent.parent.parent / "agents"


def _message(tmp_path: Path, mode: ApprovalMode | None) -> str:
    config = AgentConfig.from_yaml(AGENTS_DIR / "north" / "config.yaml")
    cls = getattr(importlib.import_module("agents.north.agent"), config.resolved_class_name)
    deps = AgentDependencies(
        context_store=FileContextStore(tmp_path / "context"),
        inference_router=MockInferenceRouter(),
        tool_registry=ToolRegistry(auto_register=False),
        confidence_tracker=ConfidenceTracker(db_path=tmp_path / "tools.db"),
        north_settings=SimpleNamespace(autonomy=mode) if mode is not None else None,
    )
    agent = cls(config, deps)
    return agent._build_task_message(AgentPayload(task_id="t", prompt="set up outreach"), "", [])


def test_in_autonomous_the_agent_is_told_north_answers_for_the_user(tmp_path) -> None:
    message = _message(tmp_path, ApprovalMode.AUTONOMOUS)

    assert "the approval mode is autonomous" in message
    assert "north decides cards for the user" in message
    assert "Never promise that nothing happens until they approve when north answers for them" in message


@pytest.mark.parametrize("mode", list(ApprovalMode))
def test_every_mode_says_who_answers(tmp_path, mode) -> None:
    message = _message(tmp_path, mode)

    assert f"the approval mode is {mode.value}: {mode.who_answers}" in message


def test_in_ask_the_user_answers(tmp_path) -> None:
    assert "the user answers every card for a change" in _message(tmp_path, ApprovalMode.ASK)


def test_with_no_settings_the_message_names_no_mode(tmp_path) -> None:
    assert "the approval mode is" not in _message(tmp_path, None)
