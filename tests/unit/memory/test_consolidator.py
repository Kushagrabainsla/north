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


class _Answering:
    """A model that answers, and keeps every prompt it was asked."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def complete(self, request):
        from types import SimpleNamespace

        self.prompts.append(request.prompt)
        return SimpleNamespace(text="Asked: ...\nDid: ...\nResult: ...\nNext time: nothing new")


class _ModelAway:
    async def complete(self, _request):
        from inference.exceptions import AllModelsRateLimitedError

        raise AllModelsRateLimitedError("No model could serve episode_consolidator - 1 cooling down (45s)")


def _task(
    task_id: str,
    prompt: str,
    *,
    workspace: str = "/Users/me/projects/app",
    ending: str = "task_completed",
    failure: str = "",
    error_type: str | None = None,
    tools: list[str] | None = None,
) -> list[LedgerEntry]:
    start = datetime.now(UTC)
    received = _entry(
        timestamp=start,
        task_id=task_id,
        source=LedgerSource.PROMPT,
        action="task_received",
        input=prompt,
        agent_output={"workspace": workspace},
    )
    work = _entry(
        timestamp=start + timedelta(seconds=1), task_id=task_id, source=LedgerSource.AGENT, action="agent_completed"
    )
    work.output = "Saved the briefing."
    work.tools_used = tools or []
    end = _entry(timestamp=start + timedelta(seconds=2), task_id=task_id, source=LedgerSource.SYSTEM, action=ending)
    end.output = failure
    end.error_type = error_type
    return [received, work, end]


async def test_a_test_run_in_a_temp_folder_is_not_kept_as_an_episode(tmp_path: Path) -> None:
    """25 of the 33 episodes that were not successes were benchmarks and live tests."""
    rows = _task(
        "t-bench", "Fix the failing test in calc.py", workspace="/private/tmp/north-bench/calc", ending="task_failed"
    )
    store = EpisodicStore(tmp_path / "episodic.db")

    await EpisodeConsolidator(_FakeLedger(rows), store, _Answering(), tmp_path).run_once()

    assert await store.recent() == []


async def test_a_task_that_failed_for_want_of_a_model_is_not_a_lesson(tmp_path: Path) -> None:
    """'Reply with the single word ok' was kept as a failure because the free allowance was spent."""
    rows = _task(
        "t-ok",
        "Reply with the single word ok",
        ending="task_failed",
        failure="planner failed after 3 attempts: No model could serve planner - 1 FREE_SPENT until 12:51 PM",
        error_type="model_unavailable",  # as the orchestrator records it, not read from the wording
    )
    store = EpisodicStore(tmp_path / "episodic.db")

    await EpisodeConsolidator(_FakeLedger(rows), store, _Answering(), tmp_path).run_once()

    assert await store.recent() == []


async def test_a_real_failure_is_kept_and_the_note_is_asked_for_its_lesson(tmp_path: Path) -> None:
    rows = _task(
        "t-brief",
        "Compile today's research briefing",
        ending="task_failed",
        failure="write_file refused: path outside the workspace",
        tools=["fetch_url", "write_file"],
    )
    store = EpisodicStore(tmp_path / "episodic.db")
    model = _Answering()

    await EpisodeConsolidator(_FakeLedger(rows), store, model, tmp_path).run_once()

    assert [episode["outcome"] for episode in await store.recent()] == ["failed"]
    asked = model.prompts[0]
    assert "Its outcome: failed." in asked and "Next time:" in asked
    assert "Tools used: fetch_url, write_file" in asked
    assert "write_file refused: path outside the workspace" in asked


async def test_with_the_model_away_the_note_waits_instead_of_saving_a_cut_off(tmp_path: Path) -> None:
    rows = _task("t-brief", "Compile today's research briefing")
    store = EpisodicStore(tmp_path / "episodic.db")
    consolidator = EpisodeConsolidator(_FakeLedger(rows), store, _ModelAway(), tmp_path)

    assert await consolidator.run_once() == 0
    assert await store.recent() == []

    consolidator._inference_router = _Answering()  # the model is back
    assert await consolidator.run_once() == 1
