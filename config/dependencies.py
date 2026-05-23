"""Dependency injection wire-up.

See docs/CODING_STYLE.md Section 6.3.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from approval import MacOSNotifier, Notifier, TerminalNotifier
from config.settings import settings
from context import ContextStore, FileContextStore
from inference import (
    CompletionRequest,
    CompletionResponse,
    InferenceRouter,
    ModelPool,
    OpenRouterInferenceRouter,
    PoolPriority,
    TranscriptionRequest,
    TranscriptionResponse,
)
from jobs import JobProcessor, SQLiteJobProcessor
from ledger import LedgerWriter, SQLiteLedgerWriter


class MockInferenceRouter(InferenceRouter):
    """Mock implementation of the InferenceRouter for dev and tests."""

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        """Simulate a chat completion."""
        return CompletionResponse(
            text="Mocked completion response.",
            model_used="mock-model",
            tokens_in=10,
            tokens_out=20,
            cost_usd=0.0001,
        )

    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResponse:
        """Simulate an audio transcription."""
        return TranscriptionResponse(
            text="Mocked transcription text.",
            model_used="mock-transcription-model",
            cost_usd=0.0,
        )

    async def get_model(self, priority: PoolPriority) -> str:
        """Retrieve the primary model name for a priority."""
        return f"mock-{priority.value}-model"

    async def refresh_pools(self) -> None:
        """Mock refresh pools."""
        pass

    def current_pools(self) -> dict[str, ModelPool]:
        """Return the current mocked pools."""
        return {
            "reasoning": ModelPool(name="reasoning", models=["mock-reasoning-model"]),
            "fast_cheap": ModelPool(name="fast_cheap", models=["mock-fast_cheap-model"]),
            "high_volume": ModelPool(name="high_volume", models=["mock-high_volume-model"]),
        }


@dataclass
class Dependencies:
    """Dependency container for injecting concrete implementations into interfaces."""

    context_store: ContextStore
    ledger: LedgerWriter
    inference_router: InferenceRouter
    notifier: Notifier
    job_processor: JobProcessor


def build_production_dependencies() -> Dependencies:
    """Build production dependencies wired once at startup."""
    return Dependencies(
        context_store=FileContextStore(settings.north_home / "context"),
        ledger=SQLiteLedgerWriter(settings.north_home / "ledger.db"),
        inference_router=OpenRouterInferenceRouter(
            settings.openrouter_api_key,
            settings.north_home / "inference_cache.json",
        ),
        notifier=MacOSNotifier(settings.secret),
        job_processor=SQLiteJobProcessor(settings.north_home / "jobs.db"),
    )


def build_test_dependencies(tmp_path: Path) -> Dependencies:
    """Build test dependencies with isolated paths and mocks."""
    return Dependencies(
        context_store=FileContextStore(tmp_path / "context"),
        ledger=SQLiteLedgerWriter(tmp_path / "ledger.db"),
        inference_router=MockInferenceRouter(),
        notifier=TerminalNotifier(),
        job_processor=SQLiteJobProcessor(tmp_path / "jobs.db"),
    )
