"""Context document API routes expose the effective persona."""

from pathlib import Path

import pytest

import orchestrator.api_router as api
from memory import FileContextStore


@pytest.mark.asyncio
async def test_read_context_soul_uses_shipped_default_when_override_missing(tmp_path: Path) -> None:
    previous = api._context_store
    api._context_store = FileContextStore(tmp_path / "context")
    try:
        result = await api.read_context("soul.md")
    finally:
        api._context_store = previous

    assert result.document == "soul.md"
    assert "You are north" in result.content


@pytest.mark.asyncio
async def test_read_context_soul_prefers_user_override(tmp_path: Path) -> None:
    store = FileContextStore(tmp_path / "context")
    await store.write(api.ContextDocument.SOUL, "Custom persona")
    previous = api._context_store
    api._context_store = store
    try:
        result = await api.read_context("soul")
    finally:
        api._context_store = previous

    assert result.content == "Custom persona"
