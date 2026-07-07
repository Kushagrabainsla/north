"""Tests for dispatcher response validation - empty / json-ignoring models fall through."""

from __future__ import annotations

import pytest

from inference.capability import ModelCapability, ModelInfo
from inference.dispatcher import ModelDispatcher, _completion_has_text, _toolcall_has_output
from inference.models import CompletionRequest, CompletionResponse, PoolPriority


def _resp(text: str, model: str) -> CompletionResponse:
    return CompletionResponse(text=text, model_used=model, tokens_in=1, tokens_out=1, cost_usd=0.0)


def test_completion_has_text():
    assert _completion_has_text(_resp("hi", "m")) is True
    assert _completion_has_text(_resp("   ", "m")) is False
    assert _completion_has_text(_resp("", "m")) is False


def test_toolcall_has_output():
    class R:
        def __init__(self, calls, content):
            self.calls = calls
            self.content = content

    assert _toolcall_has_output(R(["c"], None)) is True
    assert _toolcall_has_output(R([], "answer")) is True
    assert _toolcall_has_output(R([], "  ")) is False
    assert _toolcall_has_output(R([], None)) is False


class _FakeProvider:
    def __init__(self, name: str, model_id: str, quality: float, responder) -> None:
        self._name = name
        self._model_id = model_id
        self._quality = quality
        self._responder = responder
        self.calls: list[str] = []

    @property
    def name(self) -> str:
        return self._name

    def get_models(self) -> dict[str, ModelInfo]:
        return {
            self._model_id: ModelInfo(
                model_id=self._model_id,
                provider_name=self._name,
                capabilities=frozenset({ModelCapability.COMPLETION}),
                context_window=100_000,
                cost_per_token=0.0,
                base_quality=self._quality,
            )
        }

    async def complete(self, model_id: str, request: CompletionRequest) -> CompletionResponse:
        self.calls.append(model_id)
        return self._responder(model_id, request)

    async def refresh(self) -> None:
        pass


@pytest.mark.asyncio
async def test_json_ignoring_model_is_skipped(tmp_path):
    # 'bad' ranks first but returns a <thought> trace instead of JSON; 'good' returns JSON.
    bad = _FakeProvider("p", "bad", quality=0.99, responder=lambda m, r: _resp("<thought>not json</thought>", m))
    good = _FakeProvider("p", "good", quality=0.5, responder=lambda m, r: _resp('{"ok": true}', m))
    disp = ModelDispatcher(providers=[bad, good], cooldowns_path=tmp_path / "cd.json")

    resp = await disp.complete(
        CompletionRequest(prompt="classify", priority=PoolPriority.HIGH, component="planner", json_mode=True)
    )
    assert resp.text == '{"ok": true}'
    assert bad.calls == ["bad"]  # tried the top model, found it invalid
    assert good.calls == ["good"]  # fell through to the JSON-honouring one


@pytest.mark.asyncio
async def test_empty_completion_is_skipped(tmp_path):
    empty = _FakeProvider("p", "empty", quality=0.99, responder=lambda m, r: _resp("   ", m))
    good = _FakeProvider("p", "good", quality=0.5, responder=lambda m, r: _resp("hello", m))
    disp = ModelDispatcher(providers=[empty, good], cooldowns_path=tmp_path / "cd.json")

    resp = await disp.complete(CompletionRequest(prompt="hi", priority=PoolPriority.HIGH, component="planner"))
    assert resp.text == "hello"
