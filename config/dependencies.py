"""Dependency injection wire-up.

See docs/CODING_STYLE.md Section 6.3.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from approval import Notifier, TerminalNotifier
from config.settings import settings
from config.strategy import NorthSettings
from context import ContextStore, FileContextStore

from inference import (
    InferenceRouter,
    OpenRouterInferenceRouter,
)
from jobs import JobProcessor, SQLiteJobProcessor
from ledger import LedgerWriter, SQLiteLedgerWriter


@dataclass
class Dependencies:
    """Dependency container for injecting concrete implementations into interfaces."""

    context_store: ContextStore
    ledger: LedgerWriter
    inference_router: InferenceRouter
    notifier: Notifier
    job_processor: JobProcessor


def build_production_dependencies(north_settings: NorthSettings | None = None) -> Dependencies:
    """Build production dependencies wired once at startup."""
    return Dependencies(
        context_store=FileContextStore(settings.north_home / "context"),
        ledger=SQLiteLedgerWriter(settings.north_home / "ledger.db"),
        inference_router=OpenRouterInferenceRouter(
            settings.openrouter_api_key,
            settings.north_home / "inference_cache.json",
            north_settings=north_settings,
        ),
        notifier=TerminalNotifier(),
        job_processor=SQLiteJobProcessor(settings.north_home / "jobs.db"),
    )
