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


async def test_each_tool_call_saves_where_its_time_went(tmp_path: Path) -> None:
    """Issue #83: one event per call with its total time and the steps inside it."""
    from tools.base import Tool
    from tools.models import ToolInput, ToolOutput
    from utils.spans import span

    class _Stream:
        def __init__(self) -> None:
            self.events: list[tuple[str, dict]] = []

        async def emit(self, task_id: str, event: str, data: dict) -> None:
            self.events.append((event, data))

    class _Stepping(Tool):
        name = "stepping"
        description = "has one inner step"

        async def run(self, inp: ToolInput) -> ToolOutput:
            with span("inner"):
                await asyncio.sleep(0)
            return ToolOutput(success=True, data={})

    stream = _Stream()
    agent = _make_agent(tmp_path, stream_manager=stream)
    await agent._call_tool({"stepping": _Stepping()}, "stepping", {"action": "look"}, _payload())

    [timing] = [data for event, data in stream.events if event == "tool_timing"]
    assert timing["tool"] == "stepping"
    assert timing["action"] == "look"
    assert timing["ms"] >= 0
    assert set(timing["spans"]) == {"prepare", "run", "inner"}


async def test_a_result_with_no_text_of_its_own_is_sent_once(tmp_path: Path) -> None:
    """Issue #83: the default `formatted` text was the data's JSON again, a second copy of every result."""
    from agents.agentic_llm_agent import _tool_result_event
    from tools.base import Tool
    from tools.models import ToolInput, ToolOutput

    class _Plain(Tool):
        name = "plain"
        description = "returns data"

        async def run(self, inp: ToolInput) -> ToolOutput:
            return ToolOutput(success=True, data={"rows": [1, 2, 3]})

    agent = _make_agent(tmp_path)
    sent, _ = await agent._call_tool({"plain": _Plain()}, "plain", {}, _payload())
    assert "formatted" not in json.loads(sent)
    assert sent.count("rows") == 1
    # The terminal and the dashboard still show the data, as they did.
    assert json.loads(_tool_result_event("plain", sent, True)["formatted"]) == {"rows": [1, 2, 3]}


async def test_the_browser_sends_one_copy_without_the_profile_check(tmp_path: Path) -> None:
    """An inspect sent its page tree three times, and every result a 64-character connection_id."""
    from tools.models import ToolInput, ToolOutput
    from tools.universal.browser import BrowserTool

    tree = 'uid=n1 link "Hacker News"'
    identity = {"profile_verified": True, "connection_id": "c" * 64, "profile_id": "p1", "profile_name": "Work"}

    class _Browser(BrowserTool):
        async def execute(self, inp: ToolInput) -> ToolOutput:
            action = inp.params["action"]
            page = {"snapshot": tree, "url": "https://news.ycombinator.com/"} if action == "inspect" else {}
            return ToolOutput(success=True, data={"action": action, **identity, **page})

    agent = _make_agent(tmp_path)
    tools = {"browser": _Browser()}
    sent, _ = await agent._call_tool(tools, "browser", {"action": "inspect"}, _payload())
    assert sent.count("Hacker News") == 1
    assert "news.ycombinator.com" in sent
    assert "connection_id" not in sent and "data" not in json.loads(sent)

    checked, _ = await agent._call_tool(tools, "browser", {"action": "preflight"}, _payload())
    assert "connection_id" in checked


async def test_a_timing_record_that_cannot_be_saved_does_not_fail_the_call(tmp_path: Path) -> None:
    """The call worked; only its timing record failed, and the agent must not be told the call failed."""
    from tools.base import Tool
    from tools.models import ToolInput, ToolOutput

    class _Broken:
        async def emit(self, task_id: str, event: str, data: dict) -> None:
            if event == "tool_timing":
                raise RuntimeError("the event store is gone")

    class _Works(Tool):
        name = "works"
        description = "always works"

        async def run(self, inp: ToolInput) -> ToolOutput:
            return ToolOutput(success=True, data={"done": True})

    agent = _make_agent(tmp_path, stream_manager=_Broken())
    sent, _ = await agent._call_tool({"works": _Works()}, "works", {}, _payload())
    assert json.loads(sent)["success"] is True
