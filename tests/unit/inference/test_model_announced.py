"""The model a tool-call completion runs on is announced before the call, not after it.

The agent emitted the model event once the response was in, so "running on <model>"
showed only in the instant before "done", in the TUI, the CLI and the dashboard alike.
"""

from __future__ import annotations

import pytest

from inference.capability import ModelInfo
from inference.dispatcher import ModelDispatcher
from inference.models import ToolCallRequest, ToolCallResponse
from tests.unit.inference._catalog import publish_catalog


class _Provider:
    name = "openrouter"

    def __init__(self, model_id: str, log: list[str]) -> None:
        self.model_id = model_id
        self.log = log
        self._models = {
            model_id: ModelInfo(
                model_id=model_id,
                provider_name=self.name,
                capabilities=frozenset(["completion", "tool_calls"]),
                context_window=128000,
                cost_per_token=0.0,
                base_quality=0.8,
            )
        }

    def get_models(self) -> dict[str, ModelInfo]:
        return dict(self._models)

    async def complete_with_tools(self, model_id, request, token_callback=None):
        self.log.append(f"called {model_id}")
        return ToolCallResponse(type="message", content="ok", calls=[], model_used=model_id)


def _dispatcher(tmp_path, log: list[str]) -> ModelDispatcher:
    dispatcher = ModelDispatcher(
        [_Provider("stealth/ox-alpha", log)],
        cooldowns_path=tmp_path / "cooldowns.json",
        models_db_path=tmp_path / "models.db",
    )
    publish_catalog(dispatcher)

    async def sink(task_id: str, model_id: str) -> None:
        log.append(f"announced {model_id} for {task_id}")

    dispatcher.set_model_sink(sink)
    return dispatcher


def _request(task_id: str | None) -> ToolCallRequest:
    return ToolCallRequest(messages=[{"role": "user", "content": "hi"}], tools=[], component="general", task_id=task_id)


@pytest.mark.asyncio
async def test_the_model_is_announced_before_the_provider_is_called(tmp_path) -> None:
    log: list[str] = []
    await _dispatcher(tmp_path, log).complete_with_tools(_request("t1"))

    assert log == ["announced stealth/ox-alpha for t1", "called stealth/ox-alpha"]


@pytest.mark.asyncio
async def test_a_call_with_no_task_announces_nothing(tmp_path) -> None:
    log: list[str] = []
    await _dispatcher(tmp_path, log).complete_with_tools(_request(None))

    assert log == ["called stealth/ox-alpha"]
