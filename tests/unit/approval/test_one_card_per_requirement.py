"""Whatever an agent needs from you to do one thing is asked once, by the approval layer (CODING_STYLE §7.3).

In the issue #76 test (A14) an agent raised its own "Delete old_safe.log" card, and
once it was approved bash asked again; one delete took three cards. The agent's
card went to you directly, beside the approval layer. Now `request_approval` names
the exact calls it is for: the approval layer decides them together, on one card
when your mode asks, and those calls then run once each without another card.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from agents.agentic_llm_agent import AgenticLLMAgent
from agents.models import AgentConfig, AgentDependencies, AgentPayload
from agents.north.agent import NorthAgent
from approval.approvals import Request
from approval.interaction import UserInteraction
from approval.models import ApprovalDecision
from approval.policy import Action, ActionKind
from approval.store import ApprovalStore
from config.approval_mode import ApprovalMode
from inference.models import ToolCall
from memory import FileContextStore
from tests.conftest import MockInferenceRouter, approval_policy, approvals
from tools.base import Tool
from tools.confidence import ConfidenceTracker
from tools.models import ToolInput, ToolOutput
from tools.registry import ToolRegistry

AGENTS_DIR = Path(__file__).parents[3] / "agents"
PAYLOAD = AgentPayload(task_id="t1", prompt="delete old_safe.log, i dont need it anymore")


class _Acts(Tool):
    """Deletes a file or sends a message, as far as the approval layer can tell."""

    description = "Acts on something."
    parameters_schema = {"type": "object", "properties": {"target": {"type": "string"}}, "required": ["target"]}
    is_mutating = True

    def __init__(self, name: str, *, sends: bool = False) -> None:
        self.name = name
        self.sends = sends
        self.done: list[str] = []

    async def describe(self, input: ToolInput) -> Request:
        target = input.params["target"]
        action = Action(
            agent=self.name,
            kind=ActionKind.OTHER,
            summary=f"{self.name} {target}",
            operation=self.name,
            args=target,
            reaches_third_party=self.sends,
        )
        return Request(action=action, title=f"{self.name} {target}?", message=f"{self.name} {target}")

    async def run(self, input: ToolInput) -> ToolOutput:
        self.done.append(input.params["target"])
        return ToolOutput(success=True, data={})


def _agent(tmp_path: Path, mode: ApprovalMode, store: ApprovalStore, *tools: Tool) -> AgenticLLMAgent:
    registry = ToolRegistry(auto_register=False, approvals=approvals(mode, store=store))
    for tool in tools:
        registry.register(tool)
    deps = AgentDependencies(
        context_store=FileContextStore(tmp_path / "context"),
        inference_router=MockInferenceRouter(),
        tool_registry=registry,
        confidence_tracker=ConfidenceTracker(db_path=tmp_path / "tools.db"),
        interaction=UserInteraction(store, policy=approval_policy(mode)),
    )
    return NorthAgent(AgentConfig.from_yaml(AGENTS_DIR / "north" / "config.yaml"), deps)


async def _call(agent: AgenticLLMAgent, name: str, params: dict) -> dict:
    tools = {tool.name: tool for tool in agent._deps.tool_registry.all_tools()}
    _, result, _, _ = await asyncio.wait_for(
        agent._execute_call(ToolCall(name=name, call_id="c", params=params), PAYLOAD, tools), 2
    )
    return json.loads(result)


async def _answer_next_card(store: ApprovalStore, decision: ApprovalDecision) -> None:
    while not store.pending():
        await asyncio.sleep(0.01)
    store.resolve(store.pending()[0].id, decision, decided_by="you")


def _ahead(*calls: tuple[str, str]) -> dict:
    return {
        "title": "Delete old_safe.log",
        "message": "You said you do not need it.",
        "calls": [{"tool": tool, "params": {"target": target}} for tool, target in calls],
    }


async def test_a_delete_asked_ahead_is_one_card_and_then_runs() -> None:
    store, deletes = ApprovalStore(), _Acts("delete")
    agent = _agent(Path("/tmp"), ApprovalMode.ASK, store, deletes)

    answering = asyncio.create_task(_answer_next_card(store, ApprovalDecision.APPROVED))
    asked = await _call(agent, "request_approval", _ahead(("delete", "old_safe.log")))
    await answering
    done = await _call(agent, "delete", {"target": "old_safe.log"})

    assert asked["approved"] and done["success"] and deletes.done == ["old_safe.log"]
    assert [card.status for card in store.all()] == [ApprovalDecision.APPROVED]  # one card, nothing else


async def test_one_card_covers_every_call_a_thing_takes_even_a_send() -> None:
    """Filling a form and submitting it: a submit reaches someone else, and still asks only once."""
    store, fills, submits = ApprovalStore(), _Acts("fill"), _Acts("submit", sends=True)
    agent = _agent(Path("/tmp"), ApprovalMode.ASK, store, fills, submits)

    answering = asyncio.create_task(_answer_next_card(store, ApprovalDecision.APPROVED))
    await _call(agent, "request_approval", _ahead(("fill", "name"), ("fill", "email"), ("submit", "application")))
    await answering
    for tool, target in (("fill", "name"), ("fill", "email"), ("submit", "application")):
        assert (await _call(agent, tool, {"target": target}))["success"]

    [card] = store.all()
    assert "`fill name`" in card.message and "`submit application`" in card.message


async def test_a_call_other_than_the_ones_approved_asks_and_each_runs_only_once() -> None:
    store, deletes = ApprovalStore(), _Acts("delete")
    agent = _agent(Path("/tmp"), ApprovalMode.ASK, store, deletes)
    answering = asyncio.create_task(_answer_next_card(store, ApprovalDecision.APPROVED))
    await _call(agent, "request_approval", _ahead(("delete", "old_safe.log")))
    await answering
    await _call(agent, "delete", {"target": "old_safe.log"})

    for target in ("other.log", "old_safe.log"):  # another file, and the same delete a second time
        asking = asyncio.create_task(_call(agent, "delete", {"target": target}))
        await _answer_next_card(store, ApprovalDecision.REJECTED)
        assert not (await asking)["success"]


async def test_a_no_on_the_card_runs_nothing_and_holds() -> None:
    store, deletes = ApprovalStore(), _Acts("delete")
    agent = _agent(Path("/tmp"), ApprovalMode.ASK, store, deletes)

    answering = asyncio.create_task(_answer_next_card(store, ApprovalDecision.REJECTED))
    asked = await _call(agent, "request_approval", _ahead(("delete", "old_safe.log")))
    await answering

    assert (
        asked["approved"] is False and asked["decided_by"] == "the user" and asked["next"] == "Do not make these calls."
    )
    assert deletes.done == []


async def test_in_yolo_nothing_asks_and_the_agent_is_told_who_decided() -> None:
    store, deletes = ApprovalStore(), _Acts("delete")
    agent = _agent(Path("/tmp"), ApprovalMode.YOLO, store, deletes)

    asked = await _call(agent, "request_approval", _ahead(("delete", "old_safe.log")))
    done = await _call(agent, "delete", {"target": "old_safe.log"})

    assert asked["approved"] and done["success"]
    assert "yolo" in asked["decided_by"] and "the user has not seen" in asked["decided_by"]
    assert store.pending() == [] and len(store.all()) == 1  # one resolved record, decided by yolo


async def test_an_approval_must_name_its_calls_and_only_calls_that_can_run() -> None:
    store = ApprovalStore()
    agent = _agent(Path("/tmp"), ApprovalMode.ASK, store, _Acts("delete"))

    unnamed = await _call(agent, "request_approval", {"title": "Delete it?", "message": "?"})
    unknown = await _call(agent, "request_approval", _ahead(("shred", "old_safe.log")))
    unfit = await _call(agent, "request_approval", {"calls": [{"tool": "delete", "params": {"path": "x"}}]})

    assert "Name in `calls`" in unnamed["error"] and "not one of your tools" in unknown["error"]
    assert unfit["approved"] is False and "missing 'target'" in unfit["why"]
    assert store.all() == []  # nothing was asked
