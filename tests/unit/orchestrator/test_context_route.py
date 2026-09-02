"""Context document API routes expose the effective persona."""

from pathlib import Path

import pytest

import orchestrator.api_router as api
from memory import FileContextStore
from orchestrator.api_context import ApiServices, bind_services


@pytest.mark.asyncio
async def test_read_context_soul_uses_shipped_default_when_override_missing(tmp_path: Path) -> None:
    services = ApiServices(context_store=FileContextStore(tmp_path / "context"))
    with bind_services(services):
        result = await api.read_context("soul.md")

    assert result.document == "soul.md"
    assert "You are north" in result.content


@pytest.mark.asyncio
async def test_read_context_soul_prefers_user_override(tmp_path: Path) -> None:
    store = FileContextStore(tmp_path / "context")
    await store.write(api.ContextDocument.SOUL, "Custom persona")

    with bind_services(ApiServices(context_store=store)):
        result = await api.read_context("soul")

    assert result.content == "Custom persona"


@pytest.mark.asyncio
async def test_read_context_without_a_configured_store_fails_clearly() -> None:
    """An unwired component names itself instead of raising an AttributeError."""
    with bind_services(ApiServices()), pytest.raises(RuntimeError, match="context_store"):
        await api.read_context("soul")
