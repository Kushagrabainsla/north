"""A task does not end while a tool says something must still happen."""

from __future__ import annotations

from pathlib import Path

from agents.models import AgentConfig, AgentDependencies, AgentPayload
from agents.north.agent import NorthAgent
from inference.models import ToolCallResponse
from memory import FileContextStore
from tests.conftest import MockInferenceRouter
from tools.base import Tool
from tools.confidence import ConfidenceTracker
from tools.models import ToolInput, ToolOutput
from tools.registry import ToolRegistry

AGENTS_DIR = Path(__file__).parent.parent.parent.parent / "agents"


class _Router(MockInferenceRouter):
    """Answers in plain text each time, and keeps what it was sent."""

    def __init__(self) -> None:
        self.seen: list[list[dict]] = []

    async def complete_with_tools(self, request, token_callback=None) -> ToolCallResponse:
        self.seen.append(list(request.messages))
        return ToolCallResponse(type="message", content=f"answer {len(self.seen)}", model_used="m")


class _Holding(Tool):
    name = "holding"
    description = "says something must still happen, once"

    def __init__(self, note: str) -> None:
        self.note = note
        self.asked: list[tuple[str, str]] = []

    def task_note(self, workspace: str) -> str:
        return "use holding here"

    async def before_task_ends(self, task_id: str, workspace: str) -> str:
        self.asked.append((task_id, workspace))
        note, self.note = self.note, ""
        return note

    async def run(self, input: ToolInput) -> ToolOutput:
        return ToolOutput(success=True)


def _agent(tmp_path: Path, router: _Router) -> NorthAgent:
    deps = AgentDependencies(
        context_store=FileContextStore(tmp_path / "context"),
        inference_router=router,
        tool_registry=ToolRegistry(auto_register=False),
        confidence_tracker=ConfidenceTracker(db_path=tmp_path / "tools.db"),
    )
    return NorthAgent(AgentConfig.from_yaml(AGENTS_DIR / "north" / "config.yaml"), deps)


def _payload(**extra) -> AgentPayload:
    return AgentPayload(task_id="t1", prompt="do it", workspace="/repo", granted_workspace="/repo", **extra)


async def test_a_note_keeps_the_task_going_and_reaches_the_model(tmp_path: Path) -> None:
    router = _Router()
    tool = _Holding("review it first")

    result = await _agent(tmp_path, router)._execute(_payload(), "", [(tool, 0.9)])

    assert len(router.seen) == 2
    note = router.seen[1][-1]
    assert note["role"] == "user" and note["content"].endswith("\nreview it first")
    assert "not a message from the user" in note["content"], "a report said 'As you asked' of north's own rule"
    assert router.seen[1][-2] == {"role": "assistant", "content": "answer 1"}
    assert result["output"] == "answer 2" or "answer 2" in str(result)


async def test_with_nothing_left_the_first_answer_ends_the_task(tmp_path: Path) -> None:
    router = _Router()

    await _agent(tmp_path, router)._execute(_payload(), "", [(_Holding(""), 0.9)])

    assert len(router.seen) == 1


async def test_the_tool_is_asked_about_the_folder_the_server_granted(tmp_path: Path) -> None:
    tool = _Holding("")

    await _agent(tmp_path, _Router())._execute(_payload(), "", [(tool, 0.9)])

    assert tool.asked == [("t1", "/repo")]


async def test_a_read_only_overview_is_not_held(tmp_path: Path) -> None:
    router = _Router()
    tool = _Holding("do more")

    await _agent(tmp_path, router)._execute(_payload(execution_profile="quick_readonly"), "", [(tool, 0.9)])

    assert len(router.seen) == 1 and tool.asked == []


def test_a_tools_note_reaches_the_task_message(tmp_path: Path) -> None:
    message = _agent(tmp_path, _Router())._build_task_message(_payload(), "", [(_Holding(""), 0.9)])

    assert "- use holding here" in message


async def test_a_tool_with_a_note_is_offered_whatever_retrieval_picked(tmp_path: Path) -> None:
    class _Index:
        async def update_tools(self, documents) -> None:
            return None

        async def search_tools(self, query, top_k) -> list[str]:
            return ["filler0"]

    class _Filler(Tool):
        description = "x"

        def __init__(self, name: str) -> None:
            self.name = name

        async def run(self, input: ToolInput) -> ToolOutput:
            return ToolOutput(success=True)

    agent = _agent(tmp_path, _Router())
    registry = agent._deps.tool_registry
    for index in range(40):
        registry.register(_Filler(f"filler{index}"))
    registry.register(_Holding(""))
    agent._deps.tool_index = _Index()

    names = {tool.name for tool, _ in await agent._load_tools(_payload())}

    assert "holding" in names and "filler7" not in names
