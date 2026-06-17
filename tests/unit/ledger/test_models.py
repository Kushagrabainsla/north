"""Tests for ledger models and enums (Section 4 of the README)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from ledger import LedgerEntry, LedgerSource, LedgerStatus


def test_ledger_source_enum_matches_spec() -> None:
    expected = {
        "prompt",
        "mic",
        "cron",
        "agent",
        "async",
        "system",
        "manual_injection",
        "inference_router",
        "approval",
        "clarification",
        "webhook",
    }
    assert {s.value for s in LedgerSource} == expected


def test_ledger_status_enum_matches_spec() -> None:
    expected = {
        "completed",
        "pending",
        "failed",
        "approved",
        "rejected",
        "cancelled",
        "awaiting_input",
    }
    assert {s.value for s in LedgerStatus} == expected


def test_ledger_entry_accepts_minimal_required_fields() -> None:
    entry = LedgerEntry(
        id="abc123",
        timestamp=datetime.now(UTC),
        source=LedgerSource.PROMPT,
    )
    assert entry.id == "abc123"
    assert entry.source is LedgerSource.PROMPT
    assert entry.status is None
    assert entry.tools_used == []
    assert entry.agent_output is None


def test_ledger_entry_rejects_unknown_source() -> None:
    with pytest.raises(ValidationError):
        LedgerEntry(
            id="abc123",
            timestamp=datetime.now(UTC),
            source="not_a_real_source",  # type: ignore[arg-type]
        )


def test_ledger_entry_rejects_unknown_status() -> None:
    with pytest.raises(ValidationError):
        LedgerEntry(
            id="abc123",
            timestamp=datetime.now(UTC),
            source=LedgerSource.SYSTEM,
            status="halfway_done",  # type: ignore[arg-type]
        )
