"""A step is done only when the evidence says so; a pause is not a cancel; a read-only call changed nothing."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from types import SimpleNamespace

from agents.models import AgentResult
from coding_agents import PAUSING_FAILURES, FailureKind
from coding_agents.process import process_identity
from orchestrator import coding_run_recorder
from orchestrator.result_audit import _carry_earlier_evidence, _evidence_actions, _evidence_counts
from tools.universal.flow_runner import _contract_output

_NO_REQUIRED_FIELDS = {"type": "object", "properties": {"artifacts": {"type": "array", "items": {"type": "string"}}}}


def test_the_agents_final_json_comes_before_the_runs_own_bookkeeping() -> None:
    """The bookkeeping passed a schema with no required field, so the file list was never read."""
    result = SimpleNamespace(data={"outcome_status": "ok"}, output=json.dumps({"artifacts": ["brief.md"]}))

    assert _contract_output(result, _NO_REQUIRED_FIELDS) == {"artifacts": ["brief.md"]}


def test_a_fenced_final_answer_is_read_too() -> None:
    result = SimpleNamespace(data={}, output='```json\n{"artifacts": ["a.md"]}\n```')

    assert _contract_output(result, _NO_REQUIRED_FIELDS)["artifacts"] == ["a.md"]


def test_prose_falls_back_to_the_runs_data() -> None:
    result = SimpleNamespace(data={"artifacts": ["x.md"]}, output="All done.")

    assert _contract_output(result, _NO_REQUIRED_FIELDS) == {"artifacts": ["x.md"]}


def test_a_correction_is_judged_on_everything_the_task_did() -> None:
    first = AgentResult(
        output="a",
        summary="a",
        successful_tools=["git"],
        data={"evidence_counts": {"git": 1}, "evidence_actions": {"git:commit": 1}},
    )
    repair = AgentResult(
        output="b",
        summary="b",
        successful_tools=["bash"],
        data={"evidence_counts": {"bash": 2}, "evidence_actions": {"bash:pytest": 2}},
    )

    _carry_earlier_evidence(repair, first)

    assert repair.successful_tools == ["git", "bash"]
    assert _evidence_counts(repair) == {"git": 1, "bash": 2}
    assert _evidence_actions(repair) == {"git:commit": 1, "bash:pytest": 2}


def test_a_pause_keeps_the_run_like_any_other_freeze() -> None:
    assert FailureKind.PAUSED in PAUSING_FAILURES


def _sleeper() -> subprocess.Popen:
    return subprocess.Popen(["sleep", "30"], start_new_session=True)


def test_a_process_left_from_before_a_restart_is_stopped_when_it_is_that_runs() -> None:
    process = _sleeper()
    started = process_identity(process.pid)["started_at"]

    assert coding_run_recorder._stop_if_same(process.pid, float(started))

    assert process.wait(timeout=5) == -signal.SIGKILL


def test_a_number_given_to_another_process_is_left_running() -> None:
    process = _sleeper()
    try:
        assert coding_run_recorder._stop_if_same(process.pid, time.time() - 100_000)
        assert process.poll() is None
    finally:
        os.kill(process.pid, signal.SIGKILL)
        process.wait()
