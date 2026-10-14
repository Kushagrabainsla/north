"""The Memory page says how far north has read your messages, so a stall is visible."""

from __future__ import annotations

import pytest

from ledger import SQLiteLedgerWriter
from ledger.models import LedgerEntry, LedgerSource, LedgerStatus
from orchestrator.api_context import ApiServices, bind_services
from utils.ids import generate_id
from utils.time import utcnow
from web import api


@pytest.mark.asyncio
async def test_learning_status_reports_messages_not_read_yet(tmp_path) -> None:
    ledger = SQLiteLedgerWriter(tmp_path / "ledger.db")
    await ledger.write(
        LedgerEntry(
            id=generate_id(),
            timestamp=utcnow(),
            source=LedgerSource.PROMPT,
            task_id="t",
            input="I prefer morning meetings",
            status=LedgerStatus.COMPLETED,
        )
    )

    with bind_services(ApiServices(ledger=ledger, north_home=tmp_path)):
        status = await api.memory_learning()

    assert status == {"read_up_to": None, "waiting": 1, "more": False}
