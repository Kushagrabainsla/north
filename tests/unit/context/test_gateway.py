"""LocalMemoryGateway: persona loading and episode-domain scoping (2b model)."""

from __future__ import annotations

from pathlib import Path

import pytest

from memory import ContextDocument, FileContextStore, LocalMemoryGateway


@pytest.fixture
def store(tmp_path: Path) -> FileContextStore:
    return FileContextStore(tmp_path / "context")


async def test_read_persona_falls_back_to_shipped_default(store: FileContextStore) -> None:
    """With no soul.md on disk, read_persona returns the shipped default persona."""
    persona = await LocalMemoryGateway(store).read_persona()
    assert persona
    assert "north" in persona.lower()


async def test_read_persona_prefers_customized_soul(store: FileContextStore) -> None:
    """A user-edited soul.md overrides the shipped default."""
    await store.write(ContextDocument.SOUL, "Custom persona: be terse.")
    assert await LocalMemoryGateway(store).read_persona() == "Custom persona: be terse."


async def test_read_document_reads_any_document(store: FileContextStore) -> None:
    await store.write(ContextDocument.USER, "User is a pilot.")
    assert await LocalMemoryGateway(store).read_document(ContextDocument.USER) == "User is a pilot."


async def test_principal_episode_domains_are_own_plus_shared(store: FileContextStore) -> None:
    principal = await LocalMemoryGateway(store).principal_for("coder", "engineering")
    assert principal.allowed_domains == frozenset({"engineering", "general"})


async def test_principal_without_domain_reads_no_episodes(store: FileContextStore) -> None:
    principal = await LocalMemoryGateway(store).principal_for("system", None)
    assert principal.allowed_domains == frozenset()
