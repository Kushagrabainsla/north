"""A coding task sent from Telegram, end to end, against the real server and the real coding agent.

The path a user actually takes: a message in Telegram reaches the gateway, the gateway submits it to the
running server, the planner routes it to the general agent, which hands it to the installed coding agent in
edit mode; north runs the project's own tests on the change and asks, on Telegram, whether to land it. A tap
on Approve and the change is in the working tree.

Only the two ends are stand-ins. Telegram's API is a recorder with a user who taps Approve on every card, and
north's own model is scripted (route to the general agent, call `coding_agent`, report what it returned), so
the run does not depend on which model a provider serves today. Everything between them is real: the server
and its lifespan, the gateway's polling, the approval layer, the coding agent, north's test run and landing.

NORTH_LIVE_CLAUDE=1 NORTH_LIVE_CODEX=1 .venv/bin/python -m pytest tests/live/test_coding_from_telegram_live.py -q
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
from pathlib import Path

import pytest

from config.approval_mode import approve_option
from gateways.telegram import TelegramGateway
from gateways.telegram_api import parse_approval_callback
from inference.models import CompletionRequest, CompletionResponse, ToolCall, ToolCallRequest, ToolCallResponse
from tests.conftest import MockInferenceRouter

CHAT = 4242
TASK = "In src/calc.py add a function subtract(a, b) that returns a - b, and add a test for it to test_calc.py."
RUN_TIMEOUT_S = 600


def _enabled(flag: str, binary: str) -> bool:
    return os.environ.get(flag) == "1" and shutil.which(binary) is not None


class ScriptedNorth(MockInferenceRouter):
    """north's own model, scripted: plan it as engineering, hand it to the coding agent, report the result."""

    def __init__(self, backend: str) -> None:
        self._backend = backend

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        if request.component == "planner":
            plan = {"domain": "engineering", "engineering_kind": "implement", "is_consequential": False}
            return CompletionResponse(
                text=json.dumps(plan), model_used="scripted", tokens_in=1, tokens_out=1, cost_usd=0.0
            )
        return await super().complete(request)

    async def complete_with_tools(self, request: ToolCallRequest, token_callback=None) -> ToolCallResponse:
        results = [m for m in request.messages if m.get("role") == "tool"]
        if not results:
            call = ToolCall(
                name="coding_agent",
                call_id="call-1",
                params={"task": TASK, "mode": "edit", "backend": self._backend},
            )
            return ToolCallResponse(type="tool_calls", calls=[call], model_used="scripted")
        answer = f"The coding agent finished. {results[-1].get('content', '')}"
        return ToolCallResponse(type="message", content=answer, model_used="scripted")


class RecordedTelegram(TelegramGateway):
    """The gateway with Telegram's API replaced by a recorder, and a user who taps Approve on every card."""

    def __init__(self, base: str) -> None:
        super().__init__(orchestrator_base=base)
        self.sent: list[dict] = []
        self.cards: list[str] = []
        self._message_ids = 1000

    async def _send_message(self, chat_id, text, reply_to=None, reply_markup=None):
        self._message_ids += 1
        self.sent.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup, "id": self._message_ids})
        buttons = [b for row in (reply_markup or {}).get("inline_keyboard", []) for b in row]
        choices = [b for b in buttons if parse_approval_callback(b.get("callback_data", ""))]
        if choices:
            # The button a person reads as yes: "✅ Approve" on a yes/no card, the approving option otherwise.
            labels = [parse_approval_callback(b["callback_data"])[0] for b in choices]
            yes = "approved" if "approved" in labels else approve_option(labels)
            button = choices[labels.index(yes)]
            self.cards.append(text)
            asyncio.get_running_loop().create_task(self._tap(chat_id, self._message_ids, text, button))
        return {"message_id": self._message_ids}

    async def _tap(self, chat_id: int, message_id: int, text: str, button: dict) -> None:
        await self._process_callback_query(
            {
                "id": f"cb-{message_id}",
                "from": {"id": chat_id},
                "message": {"chat": {"id": chat_id}, "message_id": message_id, "text": text},
                "data": button["callback_data"],
            }
        )

    async def _edit_message_text(self, chat_id, message_id, text, reply_markup=None) -> bool:
        return True

    async def _answer_callback_query(self, callback_query_id, text=None) -> None:
        return None

    async def _send_chat_action(self, chat_id, action="typing") -> None:
        return None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def server(monkeypatch, project: Path, request):
    """The real app on a real port, its workspace the throwaway project, its model scripted."""
    import uvicorn

    import config.dependencies
    from config.settings import settings

    monkeypatch.setattr(settings, "north_env", "test")
    monkeypatch.setattr(settings, "north_workspace", str(project))
    monkeypatch.setattr(settings, "telegram_bot_token", "test-token")
    monkeypatch.setattr(settings, "telegram_allowed_chat_ids", str(CHAT))
    scripted = ScriptedNorth(request.param)
    monkeypatch.setattr(config.dependencies, "build_inference_router_from_settings", lambda **_: scripted)

    port = _free_port()
    monkeypatch.setattr(settings, "north_orchestrator_url", f"http://127.0.0.1:{port}")
    telegram = RecordedTelegram(f"http://127.0.0.1:{port}")

    async def to_the_chat(http, chat_id, text, reply_to=None, reply_markup=None):
        return await telegram._send_message(chat_id, text, reply_to=reply_to, reply_markup=reply_markup)

    # Cards reach Telegram from the server's own notifier, not the gateway: both go to the one chat.
    monkeypatch.setattr("approval.telegram.send_message", to_the_chat)

    from orchestrator.app import app

    uv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
    serving = asyncio.create_task(uv.serve())
    while not uv.started:
        if serving.done():
            serving.result()
        await asyncio.sleep(0.1)
    yield telegram
    await telegram.stop()
    uv.should_exit = True
    await serving


async def _story(telegram: RecordedTelegram) -> str:
    """What the chat saw, then the task's ledger: enough to see where a stuck run stopped."""
    lines = [f"- {m['text'][:300]!r}" for m in telegram.sent]
    tasks = [t for p in telegram._pending.values() if (t := p.get("task_id"))]
    for task_id in tasks:
        for entry in reversed(await telegram._ledger_entries(task_id)):
            lines.append(f"  ledger {entry.get('action')} {entry.get('status')} {str(entry.get('output'))[:200]!r}")
    return "\n".join(lines)


def _git(project: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=project, capture_output=True, text=True, check=True).stdout


@pytest.mark.parametrize(
    "server",
    [
        pytest.param(
            "claude",
            marks=pytest.mark.skipif(not _enabled("NORTH_LIVE_CLAUDE", "claude"), reason="NORTH_LIVE_CLAUDE=1"),
        ),
        pytest.param(
            "codex",
            marks=pytest.mark.skipif(not _enabled("NORTH_LIVE_CODEX", "codex"), reason="NORTH_LIVE_CODEX=1"),
        ),
    ],
    indirect=True,
)
async def test_a_coding_task_from_telegram_lands_after_one_tap(server: RecordedTelegram, project: Path) -> None:
    telegram = server
    try:
        await asyncio.wait_for(
            telegram._process_message({"chat": {"id": CHAT}, "from": {"id": CHAT}, "message_id": 1, "text": TASK}),
            timeout=RUN_TIMEOUT_S,
        )
    except TimeoutError:
        pytest.fail(f"no answer in {RUN_TIMEOUT_S}s. The chat saw:\n" + await _story(telegram))

    replies = [m["text"] for m in telegram.sent if not m["reply_markup"]]
    calc = (project / "src" / "calc.py").read_text()
    assert "def subtract" in calc, f"the change did not land.\ncards: {telegram.cards}\nreplies: {replies}"
    assert "subtract" in (project / "test_calc.py").read_text()
    assert any("land" in card.lower() or "apply" in card.lower() for card in telegram.cards), telegram.cards
    assert replies and "timed out" not in replies[-1].lower(), replies
    assert "src/calc.py" in _git(project, "status", "--short"), "landed as uncommitted changes"
    tests = subprocess.run(
        [str(project / ".venv" / "bin" / "python"), "-m", "pytest", "-q"], cwd=project, capture_output=True, text=True
    )
    assert tests.returncode == 0, tests.stdout + tests.stderr
