"""Approval feedback is preserved in task episodic memory."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from ledger.models import LedgerEntry, LedgerSource, LedgerStatus
from memory.consolidator import EpisodeConsolidator
from memory.episodic import EpisodicStore


def _entry(
    *,
    timestamp: datetime,
    task_id: str,
    source: LedgerSource,
    action: str,
    input: str = "",
    agent_output: dict[str, object] | None = None,
) -> LedgerEntry:
    return LedgerEntry(
        id=f"{action}-{timestamp.timestamp()}",
        timestamp=timestamp,
        source=source,
        task_id=task_id,
        action=action,
        input=input,
        agent_output=agent_output,
        status=LedgerStatus.COMPLETED,
    )


async def test_approval_feedback_is_included_in_episode_result() -> None:
    rows = [
        _entry(
            timestamp=datetime.now(UTC),
            task_id="task-1",
            source=LedgerSource.APPROVAL,
            action="approval_responded: rejected",
            input="question: Apply for the role",
            agent_output={"source": "job-applications", "decision": "rejected", "reason": "The role is too junior."},
        )
    ]

    result = EpisodeConsolidator._extract_result(rows, "partial")

    assert "The role is too junior." in result
    assert "job-applications" in result


async def test_unanswered_approval_is_not_learned_as_feedback() -> None:
    rows = [
        _entry(
            timestamp=datetime.now(UTC),
            task_id="task-1",
            source=LedgerSource.APPROVAL,
            action="approval_responded: timeout_rejected",
            agent_output={
                "source": "job-applications",
                "decision": "timeout_rejected",
                "reason": "Nobody answered.",
            },
        )
    ]

    assert EpisodeConsolidator._extract_approval_feedback(rows) == []


async def test_late_approval_rebuilds_existing_task_episode(tmp_path: Path) -> None:
    task_id = "task-1"
    start = datetime.now(UTC)
    ledger = _FakeLedger(
        [
            _entry(
                timestamp=start,
                task_id=task_id,
                source=LedgerSource.PROMPT,
                action="task_received",
                input="Prepare a job application.",
            ),
            _entry(
                timestamp=start + timedelta(seconds=1),
                task_id=task_id,
                source=LedgerSource.SYSTEM,
                action="task_completed",
            ),
        ]
    )
    store = EpisodicStore(tmp_path / "episodic.db")
    consolidator = EpisodeConsolidator(ledger, store, _FailingInference(), tmp_path, poll_interval_seconds=1)

    assert await consolidator.run_once() == 1
    ledger.entries.append(
        _entry(
            timestamp=start + timedelta(seconds=2),
            task_id=task_id,
            source=LedgerSource.APPROVAL,
            action="approval_responded: rejected",
            input="question: Apply for the role",
            agent_output={"source": "job-applications", "decision": "rejected", "reason": "The role is too junior."},
        )
    )

    assert await consolidator.run_once() == 1
    recent = await store.recent()
    assert "The role is too junior." in recent[0]["summary"]


class _FailingInference:
    async def complete(self, _request):
        raise RuntimeError("offline")


class _FakeLedger:
    def __init__(self, entries: list[LedgerEntry]) -> None:
        self.entries = entries

    async def query(self, filters):
        entries = [entry for entry in self.entries if filters.task_id is None or entry.task_id == filters.task_id]
        if filters.since is not None:
            entries = [entry for entry in entries if entry.timestamp >= filters.since]
        entries.sort(key=lambda entry: entry.timestamp, reverse=not filters.order_asc)
        return entries[: filters.limit]
