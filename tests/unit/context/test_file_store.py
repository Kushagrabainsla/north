"""Tests for FileContextStore - read, write, append, search behavior."""

from __future__ import annotations

from pathlib import Path

import pytest

from memory import ContextDocument, FileContextStore


@pytest.fixture
def store(tmp_path: Path) -> FileContextStore:
    return FileContextStore(tmp_path / "context")


async def test_read_missing_document_returns_empty_string(store: FileContextStore) -> None:
    assert await store.read(ContextDocument.PUBLIC) == ""


async def test_write_then_read_round_trips(store: FileContextStore) -> None:
    content = "I'm a student.\nI like coffee."
    await store.write(ContextDocument.PUBLIC, content)
    assert await store.read(ContextDocument.PUBLIC) == content


async def test_write_overwrites_existing_content(store: FileContextStore) -> None:
    await store.write(ContextDocument.PRIVATE, "Old content")
    await store.write(ContextDocument.PRIVATE, "New content")
    assert await store.read(ContextDocument.PRIVATE) == "New content"


async def test_append_separates_entries_with_single_newline(
    store: FileContextStore,
) -> None:
    await store.write(ContextDocument.JUDGEMENT_RULES, "Rule 1")
    await store.append(ContextDocument.JUDGEMENT_RULES, "Rule 2")
    await store.append(ContextDocument.JUDGEMENT_RULES, "Rule 3")
    assert await store.read(ContextDocument.JUDGEMENT_RULES) == "Rule 1\nRule 2\nRule 3"


async def test_append_to_missing_document_creates_it_without_leading_newline(
    store: FileContextStore,
) -> None:
    await store.append(ContextDocument.NORTH_STARS, "Become a great engineer")
    assert await store.read(ContextDocument.NORTH_STARS) == "Become a great engineer"


async def test_documents_are_stored_independently(store: FileContextStore) -> None:
    await store.write(ContextDocument.PUBLIC, "public content")
    await store.write(ContextDocument.PRIVATE, "private content")
    assert await store.read(ContextDocument.PUBLIC) == "public content"
    assert await store.read(ContextDocument.PRIVATE) == "private content"


async def test_search_returns_empty_string_when_no_docs_exist(tmp_path: Path) -> None:
    """search() on an empty store returns '' rather than raising."""
    store = FileContextStore(tmp_path / "context")
    result = await store.search("anything")
    assert result == ""


def test_constructor_creates_missing_base_directory(tmp_path: Path) -> None:
    base = tmp_path / "nested" / "context"
    assert not base.exists()
    FileContextStore(base)
    assert base.exists() and base.is_dir()


def test_constructor_is_idempotent_with_existing_directory(tmp_path: Path) -> None:
    base = tmp_path / "context"
    base.mkdir()
    (base / "public.md").write_text("preexisting", encoding="utf-8")
    store = FileContextStore(base)
    # the existing file survives construction
    import asyncio

    assert asyncio.run(store.read(ContextDocument.PUBLIC)) == "preexisting"
