"""A chat turn knows what north did in the earlier ones (issue #82, a small item).

Each turn is a new task that sees only the earlier turns' text, and only the last
five. Nine turns into a chat about a study guide, north said it had "not opened your
study guide file in this conversation"; it had read it in the first turn.
"""

from __future__ import annotations

from types import SimpleNamespace

from orchestrator.api_context import ApiServices, bind_services
from orchestrator.models import TaskResponse
from web import api as web_api
from web.conversations import ConversationStore

_GUIDE = "/Users/me/Downloads/EXAM2_STUDY_GUIDE.html"


class _Runs:
    """The tool calls each task made, as the agent run store records them."""

    def __init__(self, calls: dict[str, list[tuple[str, dict]]]) -> None:
        self.calls = calls

    async def list_for_task(self, task_id: str):
        return [SimpleNamespace(run_id=f"run-{task_id}")] if task_id in self.calls else []

    async def list_events(self, run_id: str):
        task_id = run_id.removeprefix("run-")
        return [
            {"event": "tool_called", "data": {"tool": tool, "params": params}} for tool, params in self.calls[task_id]
        ]


class _Orchestrator:
    def __init__(self) -> None:
        self.submitted: list = []

    async def submit_task(self, request):
        self.submitted.append(request)
        return TaskResponse(task_id=f"t{len(self.submitted)}", status="pending", created_at="2026-01-01T00:00:00Z")

    async def send_message(self, task_id: str, text: str) -> bool:
        return False


async def _context_after(tmp_path, monkeypatch, calls) -> str:
    async def finished(task_id):
        return {"task": {"status": "completed"}, "output": f"answer of {task_id}"}

    monkeypatch.setattr(web_api, "_task_detail", finished)
    store = ConversationStore(tmp_path / "web.db")
    conversation = await store.create()
    orchestrator = _Orchestrator()
    prompts = [f"I am studying for my exam through this: file://{_GUIDE}"] + [f"question {n}" for n in range(2, 10)]
    with bind_services(ApiServices(conversation_store=store, orchestrator=orchestrator, agent_run_store=_Runs(calls))):
        for prompt in prompts:
            await web_api.create_turn(conversation.id, web_api.TurnCreate(prompt=prompt))
    return orchestrator.submitted[-1].context


async def test_the_ninth_turn_knows_the_first_turn_read_the_guide(tmp_path, monkeypatch) -> None:
    calls = {
        "t1": [("update_plan", {"steps": []}), ("bash", {"command": f"cat {_GUIDE}"})],
        "t3": [("web_search", {"query": "ergodic MDP"})],
    }

    context = await _context_after(tmp_path, monkeypatch, calls)

    earlier, recent = context.split("## Recent conversation")
    assert "## Earlier in this chat" in earlier
    assert "file://" + _GUIDE in earlier
    assert f"North used: bash cat {_GUIDE}" in earlier
    assert "update_plan" not in context
    assert "answer of t1" not in context, "older turns are kept short, without answers"
    assert "North used: web_search ergodic MDP" in earlier
    assert "answer of t8" in recent and "question 8" in recent


async def test_a_secret_in_a_command_is_hidden(tmp_path, monkeypatch) -> None:
    secret = "sk-ant-api03-" + "a" * 40
    calls = {"t8": [("bash", {"command": f"curl -H 'x-api-key: {secret}' https://api.example.com"})]}

    context = await _context_after(tmp_path, monkeypatch, calls)

    assert "North used: bash curl" in context
    assert secret not in context


async def test_without_a_run_store_the_context_is_as_before(tmp_path, monkeypatch) -> None:
    async def finished(task_id):
        return {"task": {"status": "completed"}, "output": "done"}

    monkeypatch.setattr(web_api, "_task_detail", finished)
    store = ConversationStore(tmp_path / "web.db")
    conversation = await store.create()
    orchestrator = _Orchestrator()
    with bind_services(ApiServices(conversation_store=store, orchestrator=orchestrator)):
        await web_api.create_turn(conversation.id, web_api.TurnCreate(prompt="first"))
        await web_api.create_turn(conversation.id, web_api.TurnCreate(prompt="second"))

    assert "North used" not in orchestrator.submitted[-1].context
    assert "User: first\nNorth: done" in orchestrator.submitted[-1].context
