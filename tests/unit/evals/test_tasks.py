"""Integrity checks for the benchmark task fixtures under evals/tasks/.

A coding-eval task is only meaningful if its held-out grader FAILS on the
untouched seed - otherwise north could "pass" it without doing any work. These
checks guard every task (real git + pytest, no model), so a broken or
trivially-passable task can never silently enter the scoreboard.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from evals import harness

TASKS_ROOT = Path(__file__).resolve().parents[3] / "evals" / "tasks"
_TASKS = harness.load_tasks(TASKS_ROOT)
_TASK_IDS = [t.id for t in _TASKS]


def test_tasks_cover_multiple_kinds():
    kinds = {t.kind for t in _TASKS}
    assert {"bugfix", "feature", "refactor", "debug"} <= kinds
    assert len(_TASKS) >= 12


@pytest.mark.parametrize("task_id", _TASK_IDS)
def test_grader_fails_on_unfixed_seed(task_id: str, tmp_path: Path) -> None:
    task = next(t for t in _TASKS if t.id == task_id)
    workspace = harness.prepare_workspace(task, tmp_path / task.id)
    passed, output = harness.grade(task, workspace)
    assert passed is False, f"{task_id}: held-out grader passes on the unfixed seed:\n{output}"
