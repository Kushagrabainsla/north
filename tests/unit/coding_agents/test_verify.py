"""North runs the project's own tests on the agent's changes, and reports what it found."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coding_agents import CommandVerifier, ShellResult, VerificationState, WorkTree
from coding_agents.constants import VERIFY_TIMEOUT_SECONDS


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
    assert (call["workspace"], call["task_id"], call["timeout"]) == (tree.path, "t1", VERIFY_TIMEOUT_SECONDS)


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


def _git(path: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return done.stdout.strip()


@pytest.fixture
def changed(tmp_path: Path) -> WorkTree:
    """A real copy: `a.py` says "old" on the commit it started from and "new" on the agent's branch."""
    base, copy = tmp_path / "repo", tmp_path / "copy"
    base.mkdir()
    copy.mkdir()
    _git(copy, "init", "-q", "-b", "main")
    (copy / "a.py").write_text("old\n")
    _git(copy, "add", "-A")
    _git(copy, "commit", "-qm", "seed")
    start = _git(copy, "rev-parse", "HEAD")
    _git(copy, "checkout", "-qb", "north/wt-x")
    (copy / "a.py").write_text("new\n")
    _git(copy, "commit", "-qam", "change")
    return WorkTree(str(copy), "north/wt-x", start, str(base))


class _ByCommit:
    """Answers by what `a.py` holds, so a run on the start is told apart from one on the change."""

    def __init__(self, with_change: ShellResult, without: ShellResult) -> None:
        self.with_change, self.without = with_change, without
        self.seen: list[str] = []

    async def run(self, command: str, workspace: str, *, task_id: str, timeout: int) -> ShellResult:
        text = (Path(workspace) / "a.py").read_text().strip()
        self.seen.append(text)
        return self.with_change if text == "new" else self.without


async def _judge(changed: WorkTree, shell: _ByCommit):
    return await CommandVerifier(lambda _: "pytest -q", shell).verify(changed, "t1")


async def test_failures_that_are_there_without_the_change_do_not_count_against_it(changed) -> None:
    shell = _ByCommit(
        ShellResult(1, "FAILED t.py::a - boom\n1 failed in 3.21s"),
        ShellResult(1, "FAILED t.py::a - boom\n1 failed in 9.87s"),
    )

    result = await _judge(changed, shell)

    assert result.state is VerificationState.ALREADY_FAILING
    assert shell.seen == ["new", "old"]
    assert (Path(changed.path) / "a.py").read_text() == "new\n", "and the copy is back on the change"
    assert _git(Path(changed.path), "branch", "--show-current") == "north/wt-x"


async def test_a_test_failing_before_and_after_for_different_reasons_was_failing_anyway(changed) -> None:
    shell = _ByCommit(
        ShellResult(1, "FAILED t.py::a - AttributeError: 'ECDF' object has no attribute '_eval_univariate'"),
        ShellResult(1, "FAILED t.py::a - AttributeError: 'KDE' object has no attribute 'define_support'"),
    )

    result = await _judge(changed, shell)

    assert result.state is VerificationState.ALREADY_FAILING


async def test_a_new_failure_is_named_with_its_reason(changed) -> None:
    shell = _ByCommit(ShellResult(1, "FAILED t.py::a - boom\nFAILED t.py::b - bang"), ShellResult(1, "FAILED t.py::a"))

    result = await _judge(changed, shell)

    assert "FAILED t.py::b - bang" in result.detail and "t.py::a" not in result.detail


async def test_a_suite_that_never_finishes_with_or_without_the_change_says_nothing_about_it(changed) -> None:
    timed_out = ShellResult(124, "Command timed out after 600s.")

    result = await _judge(changed, _ByCommit(timed_out, timed_out))

    assert result.state is VerificationState.SKIPPED and "did not finish" in result.detail


async def test_a_suite_the_change_made_hang_fails(changed) -> None:
    shell = _ByCommit(ShellResult(124, "Command timed out after 600s."), ShellResult(1, "FAILED t.py::a"))

    result = await _judge(changed, shell)

    assert result.state is VerificationState.FAILED


async def test_tap_failures_stay_apart_by_their_names(changed) -> None:
    shell = _ByCommit(ShellResult(1, "not ok 1 - adds\nnot ok 2 - subtracts"), ShellResult(1, "not ok 1 - adds"))

    result = await _judge(changed, shell)

    assert result.state is VerificationState.FAILED and "subtracts" in result.detail


async def test_a_failure_only_the_change_has_is_named(changed) -> None:
    shell = _ByCommit(ShellResult(1, "FAILED t.py::a\nFAILED t.py::b"), ShellResult(1, "FAILED t.py::a"))

    result = await _judge(changed, shell)

    assert result.state is VerificationState.FAILED
    assert "FAILED t.py::b" in result.detail and "t.py::a" not in result.detail


async def test_a_failing_file_that_ran_slowly_is_the_same_failure(changed) -> None:
    """Real run: two runs of unchanged code differed only in Jest's "(5.1 s)" after some files."""
    shell = _ByCommit(ShellResult(1, "FAIL test/a.ts (5.123 s)"), ShellResult(1, "FAIL test/a.ts"))

    assert (await _judge(changed, shell)).state is VerificationState.ALREADY_FAILING


async def test_a_suites_own_logging_is_not_a_failure(changed) -> None:
    shell = _ByCommit(
        ShellResult(1, 'FAIL test/a.ts\n  ● a › b\n  ● Console\nconsole.error("Failed to parse", err)\nError: boom'),
        ShellResult(1, "FAIL test/a.ts\n  ● a › b\nsomething else went wrong"),
    )

    result = await _judge(changed, shell)

    assert result.state is VerificationState.ALREADY_FAILING


@pytest.mark.parametrize(
    "line",
    [
        "FAILED t.py::b - assert 1 == 2",
        "ERROR t.py::c",
        "--- FAIL: TestB (0.00s)",
        "[FAIL] Suite when x",
        "FAIL test/b.ts",
        "  ● suite › b",
        "test b ... FAILED",
        "not ok 2 - b",
    ],
)
async def test_each_runners_report_of_a_new_failure_is_read(changed, line) -> None:
    result = await _judge(changed, _ByCommit(ShellResult(1, f"FAIL other\n{line}"), ShellResult(1, "FAIL other")))

    assert result.state is VerificationState.FAILED and "These fail only with the change" in result.detail


async def test_output_north_cannot_read_stays_a_failure(changed) -> None:
    result = await _judge(changed, _ByCommit(ShellResult(1, "it broke"), ShellResult(1, "it broke")))

    assert result.state is VerificationState.FAILED


async def test_tests_that_pass_without_the_change_mean_it_broke_them(changed) -> None:
    result = await _judge(changed, _ByCommit(ShellResult(1, "FAILED t.py::a"), ShellResult(0, "1 passed")))

    assert result.state is VerificationState.FAILED


async def test_a_comparison_that_could_not_run_leaves_it_failed(changed) -> None:
    result = await _judge(changed, _ByCommit(ShellResult(1, "FAILED t.py::a"), ShellResult(None, refused=True)))

    assert result.state is VerificationState.FAILED


async def test_passing_tests_are_not_run_a_second_time(changed) -> None:
    shell = _ByCommit(ShellResult(0, "1 passed"), ShellResult(1, "FAILED"))

    await _judge(changed, shell)

    assert shell.seen == ["new"]
