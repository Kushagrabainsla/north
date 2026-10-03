"""North runs the project's own tests on the agent's changes, and reports what it found."""

from __future__ import annotations

from pathlib import Path

import pytest

from coding_agents import CommandVerifier, ShellResult, VerificationState, WorkTree


class FakeShell:
    def __init__(self, result: ShellResult | Exception) -> None:
        self.result = result
        self.calls: list[dict] = []
        self.seen_links: list[bool] = []
        self.tree_path = ""

    async def run(self, command: str, workspace: str, *, task_id: str, timeout: int) -> ShellResult:
        self.calls.append({"command": command, "workspace": workspace, "task_id": task_id, "timeout": timeout})
        self.seen_links.append((Path(workspace) / "node_modules").is_symlink())
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.fixture
def tree(tmp_path: Path) -> WorkTree:
    base, copy = tmp_path / "repo", tmp_path / "copy"
    base.mkdir()
    copy.mkdir()
    return WorkTree(str(copy), "north/wt-x", "abc", str(base))


def _verifier(shell: FakeShell, command: str | None = "pytest -q") -> tuple[CommandVerifier, list[str]]:
    asked: list[str] = []

    def detect(workspace: str) -> str | None:
        asked.append(workspace)
        return command

    return CommandVerifier(detect, shell), asked


async def test_passing_tests_are_reported_as_passed(tree) -> None:
    shell = FakeShell(ShellResult(0, "12 passed"))
    verifier, _ = _verifier(shell)

    result = await verifier.verify(tree, "t1")

    assert (result.state, result.command) == (VerificationState.PASSED, "pytest -q")


async def test_failing_tests_are_reported_with_the_end_of_their_output(tree) -> None:
    long_output = "noise\n" * 1000 + "FAILED test_calc.py::test_sub - assert 3 == 2"
    verifier, _ = _verifier(FakeShell(ShellResult(1, long_output)))

    result = await verifier.verify(tree, "t1")

    assert result.state is VerificationState.FAILED
    assert result.detail.endswith("assert 3 == 2") and len(result.detail) <= 2000


async def test_the_command_is_found_in_the_real_repo_and_run_in_the_copy(tree) -> None:
    shell = FakeShell(ShellResult(0))
    verifier, asked = _verifier(shell)

    await verifier.verify(tree, "t1")

    assert asked == [tree.base]
    call = shell.calls[0]
    assert (call["workspace"], call["task_id"], call["timeout"]) == (tree.path, "t1", 300)


@pytest.mark.parametrize(
    ("result", "state", "words"),
    [
        (ShellResult(None, refused=True), VerificationState.DECLINED, "did not let"),
        (ShellResult(127, "command not found"), VerificationState.SKIPPED, "not installed"),
        (ShellResult(1, "/repo/.venv/bin/python: No module named pytest"), VerificationState.SKIPPED, "not installed"),
        (ShellResult(None, error="timed out after 300s"), VerificationState.SKIPPED, "timed out"),
    ],
)
async def test_a_run_that_could_not_happen_is_never_a_failure(tree, result, state, words) -> None:
    verifier, _ = _verifier(FakeShell(result))

    outcome = await verifier.verify(tree, "t1")

    assert outcome.state is state and words in outcome.detail


async def test_a_project_with_no_test_command_is_skipped_without_running_anything(tree) -> None:
    shell = FakeShell(ShellResult(0))
    verifier, _ = _verifier(shell, command=None)

    result = await verifier.verify(tree, "t1")

    assert result.state is VerificationState.SKIPPED and not shell.calls


async def test_the_installed_dependencies_are_linked_in_only_while_the_tests_run(tree) -> None:
    (Path(tree.base) / "node_modules").mkdir()
    shell = FakeShell(ShellResult(0))
    verifier, _ = _verifier(shell, command="npm test --silent")

    await verifier.verify(tree, "t1")

    assert shell.seen_links == [True]
    assert not (Path(tree.path) / "node_modules").exists(), "and gone again afterwards"


async def test_the_link_is_removed_even_when_the_run_blows_up(tree) -> None:
    (Path(tree.base) / "node_modules").mkdir()
    verifier, _ = _verifier(FakeShell(RuntimeError("boom")), command="npm test --silent")

    with pytest.raises(RuntimeError):
        await verifier.verify(tree, "t1")

    assert not (Path(tree.path) / "node_modules").exists()


async def test_a_dependency_directory_the_copy_already_has_is_left_alone(tree) -> None:
    (Path(tree.base) / "node_modules").mkdir()
    own = Path(tree.path) / "node_modules"
    own.mkdir()
    (own / "keep").write_text("x")
    verifier, _ = _verifier(FakeShell(ShellResult(0)), command="npm test --silent")

    await verifier.verify(tree, "t1")

    assert (own / "keep").read_text() == "x"
