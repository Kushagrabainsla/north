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


async def test_legacy_markdown_is_imported_only_once(tmp_path: Path) -> None:
    legacy = tmp_path / "context"
    legacy.mkdir()
    (legacy / "soul.md").write_text("Legacy persona", encoding="utf-8")
    db_path = tmp_path / "memory.db"

    store = SQLiteContextStore(db_path, legacy_path=legacy)
    assert await store.read(ContextDocument.SOUL) == "Legacy persona"
    await store.delete(ContextDocument.SOUL)

    restarted = SQLiteContextStore(db_path, legacy_path=legacy)
    assert await restarted.read(ContextDocument.SOUL) == ""
