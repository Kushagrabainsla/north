"""The runner: choosing an agent, recording the run before and after it, and never hiding a failure."""

from __future__ import annotations

import asyncio

import pytest

from coding_agents import (
    Availability,
    BackendUnavailableError,
    CodingBackend,
    CodingRunner,
    EventKind,
    FailureKind,
    LiveRun,
    RunEvent,
    RunOutcome,
    RunSpec,
)

from .conftest import MemoryRecorder


class ScriptedBackend(CodingBackend):
    provider = "scripted"

    def __init__(self, name: str = "claude", *, available: bool = True, outcome: RunOutcome | None = None) -> None:
        self.name = name
        self._available = available
        self._outcome = outcome or RunOutcome(ok=True, text="the plan", session_id="s")
        self.specs: list[RunSpec] = []
        self.state_when_called: dict | None = None
        self.recorder: MemoryRecorder | None = None
        self.raises: Exception | None = None
        self.hang = False

    async def probe(self) -> Availability:
        return Availability(self._available, "1.0.0", "" if self._available else f"{self.name} is missing")

    async def run(self, spec, on_event):
        self.specs.append(spec)
        if self.recorder is not None:
            self.state_when_called = {run: dict(state) for run, state in self.recorder.state.items()}
        await on_event(RunEvent(EventKind.STARTED, {"pid": 4242}))
        await on_event(RunEvent(EventKind.TOOL_USE, {"tool": "Read", "summary": "calc.py"}))
        if self.raises:
            raise self.raises
        if self.hang:
            await asyncio.sleep(3600)
        return self._outcome


def _runner(recorder: MemoryRecorder, *backends: ScriptedBackend) -> CodingRunner:
    for backend in backends:
        backend.recorder = recorder
    return CodingRunner({backend.name: backend for backend in backends}, recorder)


async def _run(runner: CodingRunner, **overrides):
    return await runner.run(task_id="t1", task="plan it", workspace="/w", **overrides)


async def test_the_run_is_written_down_before_it_starts_and_again_when_it_ends(recorder) -> None:
    backend = ScriptedBackend()
    report = await _run(_runner(recorder, backend))

    assert recorder.calls[:2] == ["start", "remember"]
    assert recorder.calls[-1] == "finish"
    assert report.outcome.ok and report.backend == "claude"
    assert recorder.started[0].agent == "coding:claude" and recorder.started[0].task_id == "t1"


async def test_the_session_id_is_saved_before_the_agent_can_crash_with_it(recorder) -> None:
    backend = ScriptedBackend()

    await _run(_runner(recorder, backend))

    (state,) = backend.state_when_called.values()
    assert state["provider"] == "scripted" and state["session_id"] == backend.specs[0].session_id
    assert state["cli_version"] == "1.0.0" and state["resumed"] is False


async def test_the_process_id_and_events_are_recorded_as_they_happen(recorder) -> None:
    await _run(_runner(recorder, ScriptedBackend()))

    (state,) = recorder.state.values()
    assert state["pid"] == 4242
    assert [event for event, _ in recorder.events] == ["started", "tool_use"]


async def test_a_task_that_finds_its_live_run_continues_that_session(recorder) -> None:
    recorder.live = LiveRun(run_id="run-9", session_id="sess-9")
    backend = ScriptedBackend()

    await _run(_runner(recorder, backend))

    assert (backend.specs[0].session_id, backend.specs[0].resume) == ("sess-9", True)
    assert recorder.started[0].run_id == "run-9"


async def test_a_fresh_task_gets_a_new_run_and_a_new_session(recorder) -> None:
    backend = ScriptedBackend()

    await _run(_runner(recorder, backend))

    spec = backend.specs[0]
    assert not spec.resume and spec.session_id and recorder.started[0].run_id


async def test_it_uses_the_first_agent_that_can_take_the_run(recorder) -> None:
    missing, present = ScriptedBackend("codex", available=False), ScriptedBackend("claude")

    report = await _run(_runner(recorder, missing, present))

    assert report.backend == "claude" and not missing.specs


async def test_when_no_agent_can_run_it_says_why_for_each(recorder) -> None:
    runner = _runner(recorder, ScriptedBackend("claude", available=False), ScriptedBackend("codex", available=False))

    with pytest.raises(BackendUnavailableError, match="claude: claude is missing; codex: codex is missing"):
        await _run(runner)

    assert recorder.calls == [], "nothing is recorded for a run that never started"


async def test_an_unknown_agent_is_refused_by_name(recorder) -> None:
    with pytest.raises(BackendUnavailableError, match="'gemini' is not a coding agent"):
        await _run(_runner(recorder, ScriptedBackend()), backend="gemini")


async def test_a_failure_is_recorded_and_returned_as_it_is_never_retried_fresh(recorder) -> None:
    lost = RunOutcome(False, "", "s", failure=FailureKind.SESSION_LOST, error="No conversation found")
    backend = ScriptedBackend(outcome=lost)
    recorder.live = LiveRun("run-9", "sess-9")

    report = await _run(_runner(recorder, backend))

    assert report.outcome.failure is FailureKind.SESSION_LOST
    assert len(backend.specs) == 1, "a lost session is not started over behind your back"
    assert recorder.finished[0][1] is lost


async def test_a_failure_to_write_an_event_down_does_not_stop_the_run(recorder) -> None:
    async def broken(*args, **kwargs) -> None:
        raise OSError("disk full")

    recorder.record = broken  # type: ignore[method-assign]

    report = await _run(_runner(recorder, ScriptedBackend()))

    assert report.outcome.ok


async def test_an_unexpected_error_in_the_backend_becomes_a_recorded_failure(recorder) -> None:
    backend = ScriptedBackend()
    backend.raises = RuntimeError("boom")

    report = await _run(_runner(recorder, backend))

    assert report.outcome.failure is FailureKind.ERROR and "boom" in report.outcome.error
    assert recorder.finished[0][1].failure is FailureKind.ERROR


async def test_cancelling_a_run_records_it_cancelled_and_lets_the_cancel_through(recorder) -> None:
    backend = ScriptedBackend()
    backend.hang = True
    task = asyncio.create_task(_run(_runner(recorder, backend)))
    while "record" not in recorder.calls:
        await asyncio.sleep(0.01)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert recorder.finished[0][1].failure is FailureKind.CANCELLED
