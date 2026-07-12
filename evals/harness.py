"""Core logic for the north coding-eval harness.

Standalone by design: this module talks to a running north server over HTTP only
(it imports no north internals), so the scoreboard keeps working even while north's
package is mid-refactor. Grading is SWE-bench style: after north finishes a task, a
HELD-OUT grading test that north never saw is copied into the resulting workspace
and run; the task passes iff that test passes.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx

# Terminal task outcomes. `fail` = north ran but the held-out grader failed (north
# got it wrong). `error`/`timeout` = north/infra could not produce a result (e.g. a
# model was unavailable) - kept distinct so an unavailable model is never counted as
# a coding failure when reading the scoreboard.
PASS = "pass"
FAIL = "fail"
ERROR = "error"
TIMEOUT = "timeout"

_DEFAULT_TASK_TIMEOUT_S = 600  # how long to wait for north to finish one task
_GIT_TIMEOUT_S = 120  # setting up a task's git workspace
_GRADE_TIMEOUT_S = 180  # running the held-out grader
_HTTP_TIMEOUT_S = 30  # a single request to north
_DEFAULT_POLL_INTERVAL_S = 3.0  # between task-status polls
_OUTPUT_TAIL_CHARS = 500  # how much grader output to keep in a failure message

_GIT_ENV = ("-c", "user.email=evals@north.local", "-c", "user.name=north-evals")


@dataclass(frozen=True)
class EvalTask:
    """One gradable coding task loaded from ``evals/tasks/<id>/``."""

    id: str
    prompt: str
    kind: str  # engineering_kind hint (bugfix/feature/test/...); for reporting only
    timeout_s: int
    grade_argv: list[str]  # argv run in the workspace to grade; a leading "python" -> sys.executable
    directory: Path  # the task dir, containing seed/ and grade/

    @property
    def seed_dir(self) -> Path:
        return self.directory / "seed"

    @property
    def grade_dir(self) -> Path:
        return self.directory / "grade"


@dataclass
class TaskResult:
    """Outcome of running one task end to end."""

    task_id: str
    outcome: str  # PASS | FAIL | ERROR | TIMEOUT
    duration_s: float
    message: str = ""

    @property
    def passed(self) -> bool:
        return self.outcome == PASS


class NorthClient(Protocol):
    """The slice of north's API the harness needs (so runs are testable with a fake)."""

    def submit(self, prompt: str, workspace: str) -> str:
        """Submit a task; return its task_id."""

    def wait(self, task_id: str, timeout_s: int) -> str:
        """Block until the task reaches a terminal status; return that status.

        Must raise TimeoutError if the task does not finish within ``timeout_s``.
        """


def load_tasks(root: Path) -> list[EvalTask]:
    """Load every task under *root* (each a dir with ``task.json`` + ``seed/`` + ``grade/``)."""
    tasks: list[EvalTask] = []
    for task_json in sorted(root.glob("*/task.json")):
        data = json.loads(task_json.read_text(encoding="utf-8"))
        directory = task_json.parent
        grade_argv = data.get("grade") or ["python", "-m", "pytest", "-q", "grade_test.py"]
        tasks.append(
            EvalTask(
                id=str(data.get("id") or directory.name),
                prompt=str(data["prompt"]),
                kind=str(data.get("kind", "")),
                timeout_s=int(data.get("timeout_s", _DEFAULT_TASK_TIMEOUT_S)),
                grade_argv=[str(a) for a in grade_argv],
                directory=directory,
            )
        )
    return tasks


def _run(argv: list[str], cwd: Path, timeout: int = _GIT_TIMEOUT_S) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv, cwd=str(cwd), capture_output=True, text=True, timeout=timeout, check=False
    )


def prepare_workspace(task: EvalTask, dest: Path) -> Path:
    """Copy the task's seed into *dest* and make it a committed git repo.

    A git repo is required because north's reviewer diffs the working tree against
    HEAD to see what the coder changed.
    """
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copytree(task.seed_dir, dest, dirs_exist_ok=True)
    _run(["git", "init", "-q"], dest)
    _run(["git", *_GIT_ENV, "add", "-A"], dest)
    _run(["git", *_GIT_ENV, "commit", "-q", "-m", "seed"], dest)
    return dest


def grade(task: EvalTask, workspace: Path) -> tuple[bool, str]:
    """Copy the held-out grader into *workspace*, run it, and return (passed, output tail).

    The grader is copied in only now - after north has finished - so north never
    sees or can tune to it.
    """
    if task.grade_dir.is_dir():
        shutil.copytree(task.grade_dir, workspace, dirs_exist_ok=True)
    argv = [sys.executable if a == "python" else a for a in task.grade_argv]
    try:
        proc = _run(argv, workspace, timeout=_GRADE_TIMEOUT_S)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return False, f"grader could not run: {exc}"
    tail = (proc.stdout + proc.stderr).strip()[-_OUTPUT_TAIL_CHARS:]
    return proc.returncode == 0, tail


def run_task(task: EvalTask, client: NorthClient, work_root: Path) -> TaskResult:
    """Run one task: prepare a fresh workspace, hand it to north, then grade it."""
    start = time.monotonic()
    workspace = prepare_workspace(task, work_root / task.id)
    try:
        task_id = client.submit(task.prompt, str(workspace))
        status = client.wait(task_id, task.timeout_s)
    except TimeoutError:
        return TaskResult(task.id, TIMEOUT, time.monotonic() - start, "north did not finish in time")
    except Exception as exc:  # north/infra problem (not a coding failure)
        return TaskResult(task.id, ERROR, time.monotonic() - start, f"north error: {exc}")

    if status == "skipped":
        # north reached a terminal state but did no work because no model was
        # available (model pool exhausted). That is an infra condition, not a coding
        # failure - exclude it like ERROR/TIMEOUT rather than grading unwritten code.
        return TaskResult(task.id, ERROR, time.monotonic() - start, "north skipped: model pool exhausted")

    passed, tail = grade(task, workspace)
    duration = time.monotonic() - start
    if passed:
        return TaskResult(task.id, PASS, duration, f"north status={status}")
    return TaskResult(task.id, FAIL, duration, f"grader failed (status={status}): {tail}")


def run_all(tasks: list[EvalTask], client: NorthClient, work_root: Path) -> list[TaskResult]:
    """Run every task sequentially (writes must never run in parallel)."""
    return [run_task(task, client, work_root) for task in tasks]


def summarize(results: list[TaskResult]) -> dict[str, object]:
    """Aggregate results into a scoreboard dict."""
    counts = {PASS: 0, FAIL: 0, ERROR: 0, TIMEOUT: 0}
    for r in results:
        counts[r.outcome] = counts.get(r.outcome, 0) + 1
    graded = counts[PASS] + counts[FAIL]  # tasks north actually produced a result for
    return {
        "total": len(results),
        "passed": counts[PASS],
        "counts": counts,
        # Pass rate over GRADED tasks (excludes error/timeout, which are infra/model
        # issues, not coding failures). None when nothing was gradable.
        "pass_rate": (counts[PASS] / graded) if graded else None,
    }


def format_report(results: list[TaskResult]) -> str:
    """Render a human-readable scoreboard."""
    s = summarize(results)
    lines = ["", "=== north coding scoreboard ==="]
    for r in results:
        mark = {PASS: "PASS", FAIL: "FAIL", ERROR: "ERR ", TIMEOUT: "TIME"}[r.outcome]
        lines.append(f"  [{mark}] {r.task_id:32} {r.duration_s:6.1f}s  {r.message[:80]}")
    rate = s["pass_rate"]
    rate_str = f"{rate * 100:.0f}%" if rate is not None else "n/a (nothing gradable)"
    c = s["counts"]
    lines.append("")
    lines.append(
        f"Pass rate: {rate_str}  ({s['passed']}/{c[PASS] + c[FAIL]} graded"
        f" | {c[ERROR]} error, {c[TIMEOUT]} timeout, {s['total']} total)"
    )
    return "\n".join(lines)


class HttpNorthClient:
    """Talks to a running north server over HTTP (the real client used by the runner)."""

    def __init__(self, base_url: str, secret: str, poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S) -> None:
        self._base = base_url.rstrip("/")
        self._headers = {"X-North-Secret": secret, "Content-Type": "application/json"}
        self._poll = poll_interval_s
        self._terminal = {"completed", "task_completed_with_failures", "failed", "cancelled", "skipped"}

    def submit(self, prompt: str, workspace: str) -> str:
        with httpx.Client(timeout=_HTTP_TIMEOUT_S) as client:
            resp = client.post(
                f"{self._base}/orchestrator/task",
                headers=self._headers,
                json={"prompt": prompt, "workspace": workspace},
            )
            resp.raise_for_status()
            return str(resp.json()["task_id"])

    def wait(self, task_id: str, timeout_s: int) -> str:
        deadline = time.monotonic() + timeout_s
        with httpx.Client(timeout=_HTTP_TIMEOUT_S) as client:
            while time.monotonic() < deadline:
                resp = client.get(f"{self._base}/orchestrator/task/{task_id}", headers=self._headers)
                resp.raise_for_status()
                status = str(resp.json().get("status", ""))
                if status in self._terminal:
                    return status
                time.sleep(self._poll)
        raise TimeoutError(f"task {task_id} did not finish within {timeout_s}s")


def load_secret(path: Path | None = None) -> str:
    """Read north's shared API secret (default ``~/.north/secret.key``)."""
    secret_file = path or (Path.home() / ".north" / "secret.key")
    return secret_file.read_text(encoding="utf-8").strip()
