"""Shared pytest fixtures for north.

Test-only utilities (MockInferenceRouter, build_test_dependencies) live here,
not in production code.  Fixtures used by more than one test module also live
here; fixtures scoped to a single module live in that module's own conftest.py.

See docs/CODING_STYLE.md Section 18.2.
"""

from __future__ import annotations

import pytest
from pathlib import Path
from typing import Callable, Awaitable

from approval.terminal import TerminalNotifier
from context import FileContextStore
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
from jobs import SQLiteJobProcessor
from ledger import SQLiteLedgerWriter


class MockInferenceRouter(InferenceRouter):
    """Deterministic stand-in for OpenRouterInferenceRouter in tests.

    Returns fixed canned responses so tests don't make real network calls.
    The default complete() response is ``{"extract": false}`` (valid JSON that
    the extraction pipeline ignores), keeping tests quiet without side effects.
    Override individual methods on a per-test instance when needed.
    """

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        return CompletionResponse(
            text='{"extract": false}',
            model_used="mock-model",
            tokens_in=10,
            tokens_out=5,
            cost_usd=0.0,
        )

    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResponse:
        return TranscriptionResponse(
            text="mocked transcription",
            model_used="mock-whisper",
            cost_usd=0.0,
        )

    async def get_model(self, priority: PoolPriority) -> str:
        return f"mock-{priority.value}-model"

    async def refresh_pools(self) -> None:
        pass

    def current_pools(self) -> dict[str, ModelPool]:
        return {
            "reasoning": ModelPool(name="reasoning", models=["mock-reasoning"]),
            "fast_cheap": ModelPool(name="fast_cheap", models=["mock-fast_cheap"]),
            "high_volume": ModelPool(name="high_volume", models=["mock-high_volume"]),
            "free_fallback": ModelPool(name="free_fallback", models=["mock-free"]),
        }

    async def complete_with_tools(
        self,
        request: ToolCallRequest,
        token_callback: Callable[[str], Awaitable[None]] | None = None,
    ) -> ToolCallResponse:
        text = "Mocked agent response."
        if token_callback is not None:
            await token_callback(text)
        return ToolCallResponse(
            type="message",
            content=text,
            calls=[],
            model_used="mock-model",
        )

    async def embed(self, request: EmbedRequest) -> EmbedResponse:
        return EmbedResponse(
            embeddings=[[0.0] * 8 for _ in request.texts],
            model_used="mock-embed",
        )


def build_test_dependencies(tmp_path: Path):
    """Return a dependency bundle wired to isolated temp paths with mock inference."""
    from dataclasses import dataclass
    from context.base import ContextStore

    @dataclass
    class TestDependencies:
        context_store: ContextStore
        ledger: SQLiteLedgerWriter
        inference_router: MockInferenceRouter
        notifier: TerminalNotifier
        job_processor: SQLiteJobProcessor

    return TestDependencies(
        context_store=FileContextStore(tmp_path / "context"),
        ledger=SQLiteLedgerWriter(tmp_path / "ledger.db"),
        inference_router=MockInferenceRouter(),
        notifier=TerminalNotifier(),
        job_processor=SQLiteJobProcessor(tmp_path / "jobs.db"),
    )


# ---------------------------------------------------------------------------
# Pytest fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_inference() -> MockInferenceRouter:
    return MockInferenceRouter()


@pytest.fixture
def test_deps(tmp_path: Path):
    """Full dependency bundle with isolated SQLite databases and mock inference."""
    return build_test_dependencies(tmp_path)
