"""User extraction keeps FactStore as the single source of truth."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from ledger.models import LedgerEntry, LedgerSource, LedgerStatus
from memory import ContextDocument, FileContextStore
from memory.extraction import ExtractionPipeline
from utils.time import utcnow


async def test_user_fact_does_not_write_user_document(tmp_path: Path) -> None:
    ledger = AsyncMock()
    inference = AsyncMock()
    inference.complete.return_value = SimpleNamespace(
        text='{"extract": true, "document": "user", "delta": "User prefers window seats."}'
    )
    facts = AsyncMock()
    facts.add_fact.return_value = True
    documents = FileContextStore(tmp_path / "context")
    pipeline = ExtractionPipeline(ledger, documents, inference, tmp_path, fact_store=facts)
    entry = LedgerEntry(
        id="entry-1",
        timestamp=utcnow(),
        source=LedgerSource.PROMPT,
        input="I prefer window seats",
        status=LedgerStatus.COMPLETED,
    )

    assert await pipeline._process_entry(entry) is True
    facts.add_fact.assert_awaited_once_with("User prefers window seats.", "user")
    assert await documents.read(ContextDocument.USER) == ""
