"""Tests for the coding-eval harness (evals/harness.py).

These use a fake north client, so they never touch a real server or model - they
exercise task loading, the full prepare→grade path (real git + pytest), and the
scoreboard aggregation, including that infra errors are kept distinct from real
coding failures.
"""

from __future__ import annotations

from pathlib import Path

from evals import harness
from evals.harness import ERROR, FAIL, PASS, TIMEOUT, TaskResult

TASKS_ROOT = Path(__file__).resolve().parents[3] / "evals" / "tasks"

_FIXED_AVERAGE = "def average(nums):\n    if not nums:\n        return 0.0\n    return sum(nums) / len(nums)\n"


def _task(task_id: str):
    return next(t for t in harness.load_tasks(TASKS_ROOT) if t.id == task_id)


class _FixClient:
    """Fake north that 'fixes' fix_average_empty by writing correct code."""

    def submit(self, prompt: str, workspace: str) -> str:
        (Path(workspace) / "calc.py").write_text(_FIXED_AVERAGE, encoding="utf-8")
        return "task_fix"

    def wait(self, task_id: str, timeout_s: int) -> str:
        return "completed"


class _NoopClient:
    def submit(self, prompt: str, workspace: str) -> str:
        return "task_noop"

    def wait(self, task_id: str, timeout_s: int) -> str:
        return "completed"


class _ErrorClient:
    def submit(self, prompt: str, workspace: str) -> str:
        raise RuntimeError("model unavailable")

    def wait(self, task_id: str, timeout_s: int) -> str:  # pragma: no cover - never reached
        return "completed"


class _TimeoutClient:
    def submit(self, prompt: str, workspace: str) -> str:
        return "task_to"

    def wait(self, task_id: str, timeout_s: int) -> str:
        raise TimeoutError("too slow")


class _SkippedClient:
    """Fake north that reaches a terminal state but did no work - model pool exhausted."""

    def submit(self, prompt: str, workspace: str) -> str:
        return "task_skip"

    def wait(self, task_id: str, timeout_s: int) -> str:
        return "skipped"


def test_load_tasks_reads_fixtures():
    ids = {t.id for t in harness.load_tasks(TASKS_ROOT)}
    assert {"fix_average_empty", "implement_titlecase", "fix_discount"} <= ids
    t = _task("fix_average_empty")
    assert t.kind == "bugfix"
    assert "average" in t.prompt.lower()


def test_run_task_passes_when_fix_applied(tmp_path):
    result = harness.run_task(_task("fix_average_empty"), _FixClient(), tmp_path)
    assert result.outcome == PASS
    assert result.passed is True


def test_run_task_fails_when_code_unchanged(tmp_path):
    # The held-out grader must fail on the untouched (still-buggy) seed.
    result = harness.run_task(_task("fix_average_empty"), _NoopClient(), tmp_path)
    assert result.outcome == FAIL


def test_run_task_error_is_distinct_from_fail(tmp_path):
    result = harness.run_task(_task("fix_average_empty"), _ErrorClient(), tmp_path)
    assert result.outcome == ERROR


def test_run_task_timeout_is_distinct(tmp_path):
    result = harness.run_task(_task("fix_average_empty"), _TimeoutClient(), tmp_path)
    assert result.outcome == TIMEOUT


def test_run_task_model_scarcity_skip_is_excluded_not_graded(tmp_path):
    # A "skipped" (model pool exhausted) task did no work: it must be ERROR
    # (excluded from the pass rate), never graded as FAIL on the unfixed seed.
    result = harness.run_task(_task("fix_average_empty"), _SkippedClient(), tmp_path)
    assert result.outcome == ERROR


def test_summarize_pass_rate_excludes_infra_failures():
    results = [
        TaskResult("a", PASS, 1.0),
        TaskResult("b", FAIL, 1.0),
        TaskResult("c", ERROR, 1.0),
        TaskResult("d", TIMEOUT, 1.0),
    ]
    s = harness.summarize(results)
    assert s["passed"] == 1
    # 1 pass / 2 graded (error + timeout are not coding failures, so excluded).
    assert s["pass_rate"] == 0.5


def test_summarize_pass_rate_none_when_nothing_gradable():
    s = harness.summarize([TaskResult("c", ERROR, 1.0), TaskResult("d", TIMEOUT, 1.0)])
    assert s["pass_rate"] is None


def test_format_report_is_readable():
    report = harness.format_report([TaskResult("t1", PASS, 2.5, "ok")])
    assert "scoreboard" in report.lower()
    assert "t1" in report
    assert "100%" in report
