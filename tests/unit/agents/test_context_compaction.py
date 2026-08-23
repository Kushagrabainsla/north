"""Tests for context compaction, token estimation, and dynamic context windows."""

from __future__ import annotations

import pytest

from agents.context_compaction import (
    COMPACTION_THRESHOLD,
    compact_history,
    compact_if_needed,
    context_window_for,
    estimate_messages_tokens,
    exchange_boundaries,
    render_exchange_for_summary,
)
from inference.base import InferenceRouter
from inference.models import (
    CompletionRequest,
    CompletionResponse,
    EmbedRequest,
    EmbedResponse,
    ModelPool,
    PoolPriority,
    ToolCallRequest,
    ToolCallResponse,
    TranscriptionRequest,
    TranscriptionResponse,
)


class DummyRouter(InferenceRouter):
    def __init__(self, windows: dict[str, int] | None = None) -> None:
        self.windows = windows or {}
        self.complete_calls: list[CompletionRequest] = []

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.complete_calls.append(request)
        return CompletionResponse(
            text="Summary of previous operations.",
            model_used="test-summary-model",
            tokens_in=100,
            tokens_out=20,
            cost_usd=0.0,
        )

    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResponse:
        raise NotImplementedError

    async def get_model(self, priority: PoolPriority) -> str:
        return "test-model"

    async def refresh_pools(self) -> None:
        pass

    def current_pools(self) -> dict[str, ModelPool]:
        return {}

    async def complete_with_tools(self, request: ToolCallRequest, token_callback=None) -> ToolCallResponse:
        raise NotImplementedError

    async def embed(self, request: EmbedRequest) -> EmbedResponse:
        raise NotImplementedError

    def get_context_window(self, model_id: str) -> int:
        return self.windows.get(model_id, 128_000)


def test_estimate_messages_tokens() -> None:
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Run the tests and fix errors."},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "function": {"name": "bash", "arguments": '{"command": "pytest -v"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "PASSED 10 tests"},
    ]
    tokens = estimate_messages_tokens(messages)
    assert tokens > 10
    assert tokens < 100


def test_context_window_for_dynamic_and_fallback() -> None:
    router = DummyRouter(windows={"custom-model-x": 64_000, "stealth/ox-alpha": 128_000})

    # Query with live router
    assert context_window_for("custom-model-x", router=router) == 64_000
    assert context_window_for("stealth/ox-alpha", router=router) == 128_000

    # Query without router (fallback table)
    assert context_window_for("gemini-1.5-pro") == 1_000_000
    assert context_window_for("claude-3-7-sonnet") == 200_000
    assert context_window_for("stealth/ox-alpha") == 128_000
    assert context_window_for("some-unknown-model") == 128_000


@pytest.mark.asyncio
async def test_compact_if_needed_triggers_at_threshold() -> None:
    # Model with small 1,000 token context window
    router = DummyRouter(windows={"small-model": 1_000})

    messages = [
        {"role": "system", "content": "System prompt"},
        {"role": "user", "content": "Task description"},
    ]
    # Add 5 exchanges with substantial output (> 800 chars each)
    for i in range(5):
        messages.extend([
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": f"call_{i}", "function": {"name": "read_file", "arguments": f'{{"path": "file_{i}.py"}}'}},
                ],
            },
            {
                "role": "tool",
                "tool_call_id": f"call_{i}",
                "content": "A" * 800,  # ~200 tokens per exchange -> total > 800 tokens (> 75% of 1,000)
            },
        ])

    initial_len = len(messages)
    await compact_if_needed(
        messages,
        tokens_in=0,  # Let estimate_messages_tokens calculate footprint
        model_used="small-model",
        inference_router=router,
        component="test_agent",
        task_id="task_123",
        keep_recent=2,
    )

    # Compaction should have fired via router.complete and condensed older exchanges
    assert len(router.complete_calls) == 1
    assert len(messages) < initial_len
    assert "Earlier context (auto-compacted)" in messages[2]["content"]


@pytest.mark.asyncio
async def test_compact_if_needed_skips_when_under_threshold() -> None:
    # Model with 128k context window -> small message list should not trigger compaction
    router = DummyRouter(windows={"large-model": 128_000})

    messages = [
        {"role": "system", "content": "System prompt"},
        {"role": "user", "content": "Task description"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call_0", "function": {"name": "test", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "call_0", "content": "small output"},
    ]

    await compact_if_needed(
        messages,
        tokens_in=50,
        model_used="large-model",
        inference_router=router,
        component="test_agent",
        task_id="task_123",
        keep_recent=2,
    )

    # No summarisation call should have happened
    assert len(router.complete_calls) == 0
    assert len(messages) == 4


def test_render_exchange_preserves_multi_round_compacted_summary() -> None:
    long_summary = (
        "## Earlier context (auto-compacted)\n"
        "- Step 1: created auth.py\n"
        "- Step 2: created database.py\n"
        "- Step 3: fixed login issue in views.py\n"
        "- Step 4: passed all test suites\n"
        + "x" * 500
    )
    messages = [
        {"role": "user", "content": long_summary},
        {"role": "assistant", "content": "Understood."},
    ]
    rendered = render_exchange_for_summary(messages)
    assert "[previous summary:" in rendered
    assert long_summary in rendered
    assert len(rendered) > 500
