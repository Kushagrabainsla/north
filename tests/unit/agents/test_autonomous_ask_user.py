"""ask_user goes through the approval layer in every mode; in autonomous the memory decider answers.

The agent used to read the mode itself and, in autonomous, tell the model to
guess. Now it always asks through the one card channel, and the mode decides who
answers (#29).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from agents.agentic_llm_agent import AgenticLLMAgent
from agents.general.agent import GeneralAgent
from agents.models import AgentConfig, AgentDependencies, AgentPayload
from approval.interaction import UserInteraction
from approval.models import ApprovalDecision
from approval.store import ApprovalStore
from config.approval_mode import ApprovalMode
from memory import FileContextStore
from tests.conftest import MockInferenceRouter, StubDecider, approval_policy, deciding
from tools.confidence import ConfidenceTracker
from tools.registry import ToolRegistry

AGENTS_DIR = Path(__file__).parent.parent.parent.parent / "agents"


def _agent(tmp_path: Path, mode: ApprovalMode, decider: StubDecider, store: ApprovalStore) -> AgenticLLMAgent:
    policy = approval_policy(mode, decider=decider)
    deps = AgentDependencies(
        context_store=FileContextStore(tmp_path / "context"),
        inference_router=MockInferenceRouter(),
        tool_registry=ToolRegistry(auto_register=False),
        confidence_tracker=ConfidenceTracker(db_path=tmp_path / "tools.db"),
        interaction=UserInteraction(store, policy=policy),
    )
    config = AgentConfig.from_yaml(AGENTS_DIR / "general" / "config.yaml")
    return GeneralAgent(config, deps)


def _ask(agent: AgenticLLMAgent, question: str = "Which DB?", options=("Postgres", "SQLite")):
    return agent._ask_user(AgentPayload(task_id="t1", prompt="p"), {"question": question, "options": list(options)})


@pytest.mark.asyncio
async def test_autonomous_answers_from_memory_and_keeps_the_reason(tmp_path: Path) -> None:
    store = ApprovalStore()
    decider = deciding(ApprovalDecision.ANSWERED, "Postgres", "you use Postgres everywhere", ("fact: uses Postgres",))
    agent = _agent(tmp_path, ApprovalMode.AUTONOMOUS, decider, store)

    out = json.loads(await _ask(agent))

    assert out == {"success": True, "answered": True, "answer": "Postgres"}
    [card] = store.all()
    assert card.reason == "you use Postgres everywhere"
    assert card.memory_used == ["fact: uses Postgres"]
    assert not store.pending()


@pytest.mark.asyncio
async def test_autonomous_waits_for_you_when_the_decider_cannot_decide(tmp_path: Path) -> None:
    store = ApprovalStore()
    agent = _agent(tmp_path, ApprovalMode.AUTONOMOUS, StubDecider(None), store)

    asking = asyncio.create_task(_ask(agent))
    await asyncio.sleep(0)
    [card] = store.pending()
    store.resolve(card.id, ApprovalDecision.ANSWERED, chosen_option="SQLite")

    assert json.loads(await asking)["answer"] == "SQLite"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [ApprovalMode.ASK, ApprovalMode.SAFE])
async def test_ask_and_safe_never_let_the_decider_answer(tmp_path: Path, mode: ApprovalMode) -> None:
    store = ApprovalStore()
    decider = deciding(ApprovalDecision.ANSWERED, "Postgres")
    agent = _agent(tmp_path, mode, decider, store)

    asking = asyncio.create_task(_ask(agent))
    await asyncio.sleep(0)
    assert len(store.pending()) == 1
    assert not decider.asked
    asking.cancel()


@pytest.mark.asyncio
async def test_ask_user_requires_a_question(tmp_path: Path) -> None:
    agent = _agent(tmp_path, ApprovalMode.AUTONOMOUS, StubDecider(None), ApprovalStore())
    out = json.loads(await _ask(agent, question="  "))
    assert out["success"] is False
