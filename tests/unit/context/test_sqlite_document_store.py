"""Regression tests for database-backed context documents."""

from pathlib import Path

from memory import ContextDocument, SQLiteContextStore


async def test_sqlite_document_crud_round_trip(tmp_path: Path) -> None:
    store = SQLiteContextStore(tmp_path / "memory.db")

    await store.write(ContextDocument.NORTH_STARS, "Ship North")
    await store.append(ContextDocument.NORTH_STARS, "Make it reliable")
    assert await store.read(ContextDocument.NORTH_STARS) == "Ship North\nMake it reliable"

    await store.delete(ContextDocument.NORTH_STARS)
    assert await store.read(ContextDocument.NORTH_STARS) == ""
