"""BashShell: north's bash tool, read as the shell a coding run tests its changes with."""

from __future__ import annotations

from coding_agents import ShellResult
from tools.base import Tool
from tools.models import ToolInput, ToolOutput
from tools.registry import ToolRegistry
from tools.specialized.coding_shell import BashShell


class StubBash(Tool):
    name = "bash"
    description = "stub"

    def __init__(self, output: ToolOutput) -> None:
        self.output = output
        self.inputs: list[ToolInput] = []

    async def run(self, input: ToolInput) -> ToolOutput:
        self.inputs.append(input)
        return self.output


def _shell(output: ToolOutput) -> tuple[BashShell, StubBash]:
    bash = StubBash(output)
    registry = ToolRegistry()
    registry.register(bash)
    return BashShell(registry), bash


async def test_it_runs_the_command_in_the_copy_for_the_task_with_the_time_limit() -> None:
    shell, bash = _shell(ToolOutput(success=True, data={"stdout": "ok", "stderr": "", "returncode": 0}))

    await shell.run("pytest -q", "/copy", task_id="t1", timeout=300)

    params = bash.inputs[0].params
    assert params == {"command": "pytest -q", "workspace": "/copy", "timeout": 300, "task_id": "t1"}
    assert bash.inputs[0].granted_workspace == "/copy"


async def test_a_command_that_ran_reports_its_exit_code_and_both_streams() -> None:
    shell, _ = _shell(ToolOutput(success=False, data={"stdout": "1 failed\n", "stderr": "warn\n", "returncode": 1}))

    assert await shell.run("pytest -q", "/copy", task_id="t", timeout=1) == ShellResult(1, "1 failed\nwarn\n")


async def test_a_command_the_user_would_not_let_run_is_refused_not_failed() -> None:
    shell, _ = _shell(ToolOutput(success=False, failure_kind="refused", error="Command cancelled by user."))

    result = await shell.run("pytest -q", "/copy", task_id="t", timeout=1)

    assert result.refused and result.exit_code is None


async def test_a_suite_that_hangs_reads_as_a_failure_not_a_skip() -> None:
    shell, _ = _shell(ToolOutput(success=False, error="Command timed out after 300s."))

    result = await shell.run("pytest -q", "/copy", task_id="t", timeout=300)

    assert result.exit_code == 124 and "timed out" in result.output


async def test_a_command_that_could_not_start_has_no_exit_code() -> None:
    shell, _ = _shell(ToolOutput(success=False, error="[Errno 2] No such file or directory"))

    result = await shell.run("pytest -q", "/copy", task_id="t", timeout=1)

    assert result.exit_code is None and "No such file" in result.error
