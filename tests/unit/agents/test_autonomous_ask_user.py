"""Autonomous mode: ask_user must not block - the agent proceeds on its own."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agents.agentic_llm_agent import AgenticLLMAgent
from agents.general.agent import GeneralAgent
from agents.models import AgentConfig, AgentDependencies, AgentPayload
from approval.mode import ApprovalMode
from memory import FileContextStore
from tests.conftest import MockInferenceRouter
from tools.confidence import ConfidenceTracker
from tools.registry import ToolRegistry

AGENTS_DIR = Path(__file__).parent.parent.parent.parent / "agents"


def _agent(tmp_path: Path, mode: ApprovalMode | None) -> AgenticLLMAgent:
    deps = AgentDependencies(
        context_store=FileContextStore(tmp_path / "context"),
        inference_router=MockInferenceRouter(),
        tool_registry=ToolRegistry(graph={}, auto_register=False),
        confidence_tracker=ConfidenceTracker(db_path=tmp_path / "tools.db"),
        north_settings=SimpleNamespace(autonomy=mode) if mode is not None else None,
    )
    config = AgentConfig.from_yaml(AGENTS_DIR / "general" / "config.yaml")
    return GeneralAgent(config, deps)


@pytest.mark.asyncio
async def test_ask_user_does_not_block_in_autonomous(tmp_path: Path) -> None:
    agent = _agent(tmp_path, ApprovalMode.AUTONOMOUS)
    out = json.loads(await agent._ask_user(AgentPayload(task_id="t1", prompt="p"), {"question": "Which DB?"}))
    assert out["success"] is True
    assert out["answered"] is True
    assert "autonomous" in out["answer"].lower()


@pytest.mark.asyncio
async def test_ask_user_requires_question_even_in_autonomous(tmp_path: Path) -> None:
    agent = _agent(tmp_path, ApprovalMode.AUTONOMOUS)
    out = json.loads(await agent._ask_user(AgentPayload(task_id="t1", prompt="p"), {"question": "  "}))
    assert out["success"] is False


def test_is_autonomous_reflects_mode(tmp_path: Path) -> None:
    assert _agent(tmp_path, ApprovalMode.AUTONOMOUS)._is_autonomous() is True
    assert _agent(tmp_path, ApprovalMode.AUTO)._is_autonomous() is False
    assert _agent(tmp_path, ApprovalMode.INTERACTIVE)._is_autonomous() is False
    assert _agent(tmp_path, None)._is_autonomous() is False
