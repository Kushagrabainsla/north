"""The orchestrator takes a message for a running task into it (CODING_STYLE §15.4)."""

from __future__ import annotations

import pytest

from inference.models import ToolCallResponse
from ledger.base import LedgerFilters
from ledger.models import LedgerSource
from orchestrator.models import TaskRequest
from tests.conftest import MockInferenceRouter
from tests.integration.test_orchestrator import _make_orchestrator, _wait_for_ledger_action


class _Recording(MockInferenceRouter):
    def __init__(self) -> None:
        self.requests: list = []

    async def complete_with_tools(self, request, token_callback=None):
        self.requests.append(request)
        return ToolCallResponse(type="message", content=f"answer {len(self.requests)}", calls=[], model_used="mock")


def _wired(tmp_path):
    orch, ledger, _ = _make_orchestrator(tmp_path)
    router = _Recording()
    deps = orch._agent_registry.get("general")._deps
    deps.task_inbox = orch._inbox
    deps.inference_router = router
    return orch, ledger, router


def _user_texts(request) -> str:
    return "\n".join(str(m.get("content") or "") for m in request.messages if m.get("role") in ("user", "system"))


@pytest.mark.asyncio
async def test_a_message_sent_after_its_agents_answered_is_answered_in_the_same_task(tmp_path) -> None:
    orch, ledger, router = _wired(tmp_path)
    finish = orch._finish_task
    sent: list[bool] = []

    async def finish_after_a_late_message(task_id, **kwargs):
        if not sent:
            sent.append(await orch.send_message(task_id, "also add the totals"))
        await finish(task_id, **kwargs)

    orch._finish_task = finish_after_a_late_message
    response = await orch.submit_task(
        TaskRequest(prompt="Summarise sales", source=LedgerSource.PROMPT, forced_agent="general")
    )
    await _wait_for_ledger_action(ledger, response.task_id, "task_completed")

    entries = await ledger.query(LedgerFilters(task_id=response.task_id, limit=500))
    assert sent == [True]
    assert [e.action for e in entries].count("task_completed") == 1
    assert [e.output for e in entries if e.action == "agent_completed"].count("answer 2") == 1
    assert any(e.action == "user_message" and e.input == "also add the totals" for e in entries)
    follow_up = _user_texts(router.requests[-1])
    assert "also add the totals" in follow_up
    assert "answer 1" in follow_up  # told what the task already said, so it does not start over


@pytest.mark.asyncio
async def test_a_finished_or_unknown_task_takes_no_message(tmp_path) -> None:
    orch, ledger, _ = _wired(tmp_path)
    response = await orch.submit_task(TaskRequest(prompt="Hello", source=LedgerSource.PROMPT, forced_agent="general"))
    await _wait_for_ledger_action(ledger, response.task_id, "task_completed")
    running = orch._active_tasks.get(response.task_id)
    if running is not None:
        await running

    assert await orch.send_message(response.task_id, "hi") is False
    assert await orch.send_message("no-such-task", "hi") is False


@pytest.mark.asyncio
async def test_a_task_that_starts_over_still_has_the_messages_you_sent(tmp_path) -> None:
    orch, ledger, _ = _wired(tmp_path)
    orch._active_tasks["t1"] = None  # type: ignore[assignment]  # in flight, as far as send_message can tell
    assert await orch.send_message("t1", "use the 2025 numbers")

    restarted, _, _ = _make_orchestrator(tmp_path)  # a new process over the same ledger
    await restarted._open_inbox("t1")

    assert restarted._inbox.read("t1", 0) == ["use the 2025 numbers"]
