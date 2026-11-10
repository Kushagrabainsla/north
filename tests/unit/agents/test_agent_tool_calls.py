"""How north's agent loop answers a tool call: an unknown or failing tool, and work left for you."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from agents.agentic_llm_agent import AgenticLLMAgent
from agents.models import AgentConfig, AgentDependencies, AgentPayload
from agents.north.agent import NorthAgent
from approval.interaction import UserInteraction
from memory import FileContextStore
from tests.conftest import MockInferenceRouter
from tools.confidence import ConfidenceTracker
from tools.registry import ToolRegistry

AGENTS_DIR = Path(__file__).parent.parent.parent.parent / "agents"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_deps(tmp_path: Path, **overrides) -> AgentDependencies:
    base = AgentDependencies(
        context_store=FileContextStore(tmp_path / "context"),
        inference_router=MockInferenceRouter(),
        tool_registry=ToolRegistry(auto_register=False),
        confidence_tracker=ConfidenceTracker(db_path=tmp_path / "tools.db"),
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


def _make_agent(tmp_path: Path, **dep_overrides) -> AgenticLLMAgent:
    config = AgentConfig.from_yaml(AGENTS_DIR / "north" / "config.yaml")
    return NorthAgent(config, _make_deps(tmp_path, **dep_overrides))


def _payload() -> AgentPayload:
    return AgentPayload(task_id="t1", prompt="do something")


async def test_unknown_tool_returns_error_json(tmp_path: Path) -> None:
    """_call_tool must return structured error JSON for a tool not in tool_map."""
    agent = _make_agent(tmp_path)
    result_str, _ = await agent._call_tool({}, "nonexistent", {}, AgentPayload(task_id="t", prompt="p"))
    result = json.loads(result_str)
    assert result["success"] is False
    assert "nonexistent" in result["error"]


async def test_tool_exception_returns_error_json(tmp_path: Path) -> None:
    """_call_tool must catch exceptions from tool.run() and return error JSON."""
    from tools.base import Tool
    from tools.models import ToolInput, ToolOutput

    class ExplodingTool(Tool):
        name = "exploding"
        description = "always raises"

        def schema(self) -> dict:
            return {}

        async def run(self, inp: ToolInput) -> ToolOutput:
            raise RuntimeError("Kaboom!")

    agent = _make_agent(tmp_path)
    result_str, _ = await agent._call_tool(
        {"exploding": ExplodingTool()}, "exploding", {}, AgentPayload(task_id="t", prompt="p")
    )
    result = json.loads(result_str)
    assert result["success"] is False
    assert "Kaboom" in result["error"]


async def test_request_approval_with_wait_false_leaves_the_work_and_returns_at_once(tmp_path: Path) -> None:
    """A run preparing five applications waited on each card in turn; prepared work is left, not waited on."""
    from approval.store import ApprovalStore
    from inference.models import ToolCall

    store = ApprovalStore()
    agent = _make_agent(tmp_path, interaction=UserInteraction(store))
    call = ToolCall(
        name="request_approval",
        call_id="c1",
        params={
            "wait": False,
            "title": "Acme - Software Engineer: ready to submit",
            "message": "Filled in; the tab is open for you to submit.",
            "fields": [{"name": "email", "label": "Email", "type": "text", "value": "jane@example.com"}],
        },
    )

    _, result, success, _ = await asyncio.wait_for(agent._execute_call(call, _payload(), {}), timeout=2)

    [card] = store.pending()
    assert success and json.loads(result)["left_for_user"] is True
    assert card.blocking is False and card.outlives_task
    assert card.fields[0].value == "jane@example.com"


async def test_an_item_left_for_the_user_is_offered_once(tmp_path: Path) -> None:
    """Keeping the already-offered list was left to the agent, which lost it run after run."""
    from approval.store import ApprovalStore
    from inference.models import ToolCall
    from jobs.seen import SeenStore

    store = ApprovalStore()
    agent = _make_agent(tmp_path, interaction=UserInteraction(store), offered_store=SeenStore(tmp_path / "jobs.db"))

    def offer(*keys: str) -> ToolCall:
        return ToolCall(
            name="request_approval",
            call_id="c",
            params={"wait": False, "title": "Acme - Engineer: ready for you", "item_keys": list(keys)},
        )

    async def run(call: ToolCall) -> dict:
        _, result, _, _ = await agent._execute_call(call, _payload(), {})
        return json.loads(result)

    first = await run(offer("https://jobs.example.com/view/42/?utm_source=alert", "Acme | Engineer"))
    same_link_new_tracking = await run(offer("https://www.jobs.example.com/view/42?utm_campaign=x"))
    cross_posted = await run(offer("https://other-board.example.com/9", "acme |  engineer"))
    different_job = await run(offer("https://jobs.example.com/view/43/", "Acme | Designer"))

    assert first["left_for_user"] is True
    assert same_link_new_tracking["already_offered"] is True
    assert cross_posted["already_offered"] is True
    assert different_job["left_for_user"] is True
    assert len(store.pending()) == 2


async def test_offered_before_tells_the_agent_before_it_does_the_work(tmp_path: Path) -> None:
    """The card refuses a repeat, but only after the form was filled; the check comes first."""
    from jobs.seen import SeenStore
    from tools.models import ToolInput
    from tools.universal.offered_before import OFFERED_SCOPE, OfferedBeforeTool

    offered = SeenStore(tmp_path / "jobs.db")
    offered.remember(OFFERED_SCOPE, "https://jobs.example.com/view/42/", "Acme | Engineer")
    tool = OfferedBeforeTool(offered)

    seen = await tool.run(ToolInput(params={"item_keys": ["https://jobs.example.com/view/42?utm_source=x"]}))
    new = await tool.run(ToolInput(params={"item_keys": ["https://jobs.example.com/view/43/", "Acme | Designer"]}))

    assert seen.data == {"offered": True} and new.data == {"offered": False}
    assert not tool.mutates({"item_keys": ["x"]}), "a read, never a card"
