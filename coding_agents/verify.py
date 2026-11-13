"""Running the project's own tests on an agent's changes, ourselves.

An agent says its work passes; north does not take its word. The command comes from the project's own
markers (never from what the agent wrote), runs in the agent's copy through `Shell` (so it is ruled on and
sandboxed like any command north runs), and the result is what north reports.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from coding_agents.base import Shell, ShellResult
from coding_agents.constants import (
    LINKED_DEPENDENCY_DIRS,
    MAX_VERIFY_OUTPUT_CHARS,
    TIMED_OUT_EXIT_CODE,
    VERIFY_TIMEOUT_SECONDS,
)
from coding_agents.models import Verification, VerificationState, WorkTree

# What a shell prints when the program it was asked to run is not there.
_COULD_NOT_RUN = frozenset({126, 127})
# `python -m pytest` with no pytest exits 1 like a failing suite does; its message tells them apart.
_RUNNER_MISSING = "No module named"
# A line in which a test runner reports a failed test: pytest, unittest, Go, Ginkgo, Jest, Cargo and TAP.
# Only those: a suite's own logging is full of the word "error", and differs from one run to the next.
_FAILURE_LINE = re.compile(r"^\s*(?:-+ ?)?\[?(?:FAIL(?:ED)?|ERROR)\b|^\s*●\s(?!Console)|\.\.\. FAILED$|^\s*not ok\b")
# Jest adds "(5.1 s)" to a file only when it ran slowly, so the same failure can come with or without it.
_TIMING = re.compile(r"\s*\(\d+(?:\.\d+)?\s*m?s\)")
_NUMBER = re.compile(r"0x[0-9a-f]+|\d+(?:\.\d+)?")
# pytest's summary line: "FAILED path::test - reason". Only this form: TAP's "not ok 3 - name" is the name.
_PYTEST_REASON = re.compile(r"^((?:FAILED|ERROR) \S+) - .*$")
_ALREADY_FAILING = "the same tests fail without this change, so they say nothing about it"
_NEVER_FINISHED = (
    f"they did not finish in {VERIFY_TIMEOUT_SECONDS} s, with or without the change, so they say nothing about it"
)


class CommandVerifier:
    """Detects the project's test command and runs it in the copy."""

    def __init__(self, detect: Callable[[str], str | None], shell: Shell) -> None:
        self._detect = detect
        self._shell = shell

    async def verify(self, tree: WorkTree, task_id: str) -> Verification:
        # Detected in the real repository: that is where its virtualenv lives.
        command = self._detect(tree.base)
        if not command:
            return Verification(VerificationState.SKIPPED, detail="no test command was found for this project")
        links = _link_dependencies(tree)
        try:
            result = await self._shell.run(command, tree.path, task_id=task_id, timeout=VERIFY_TIMEOUT_SECONDS)
            verdict = _read(command, result)
            if verdict.state is VerificationState.FAILED:
                verdict = await self._against_start(verdict, result, tree, task_id)
        finally:
            _unlink(links)
        return verdict

    async def _against_start(
        self, failed: Verification, after: ShellResult, tree: WorkTree, task_id: str
    ) -> Verification:
        """Whether the change caused the failure: the same tests, in the same copy, on the commit it started from.

        A project whose suite already fails, or cannot pass on this machine, held back every change made to it.
        Anything unclear stays a failure, so a change is only let through on a like-for-like comparison.
        """
        if not await _checkout(tree.path, tree.base_sha):
            return failed
        restored = False
        try:
            before = await self._shell.run(failed.command, tree.path, task_id=task_id, timeout=VERIFY_TIMEOUT_SECONDS)
        finally:
            restored = await _checkout(tree.path, tree.branch)
        if not restored or _read(failed.command, before).state is not VerificationState.FAILED:
            return failed
        # A suite that never finishes, with or without the change, says nothing about it. One the change
        # made hang still fails: it finishes without the change.
        if after.exit_code == TIMED_OUT_EXIT_CODE and before.exit_code == TIMED_OUT_EXIT_CODE:
            return Verification(VerificationState.SKIPPED, failed.command, _NEVER_FINISHED)
        failures = _failure_lines(after.output)
        if not failures:  # a runner whose report north cannot read: nothing to compare
            return failed
        earlier = _failure_lines(before.output)
        new = [line for test, line in failures.items() if test not in earlier]
        if new:
            only_with_it = "\n".join(sorted(new))[:MAX_VERIFY_OUTPUT_CHARS]
            return replace(failed, detail=f"These fail only with the change:\n{only_with_it}")
        return Verification(VerificationState.ALREADY_FAILING, failed.command, _ALREADY_FAILING)


def _read(command: str, result: ShellResult) -> Verification:
    """What one run of the tests says."""
    if result.refused:
        return Verification(VerificationState.DECLINED, command, "you did not let the tests run")
    if result.exit_code is None:
        return Verification(VerificationState.SKIPPED, command, f"it could not be run: {result.error}".strip())
    if result.exit_code in _COULD_NOT_RUN or (result.exit_code != 0 and _RUNNER_MISSING in result.output):
        return Verification(VerificationState.SKIPPED, command, "its test runner is not installed")
    if result.exit_code == 0:
        return Verification(VerificationState.PASSED, command)
    return Verification(VerificationState.FAILED, command, result.output.strip()[-MAX_VERIFY_OUTPUT_CHARS:])


def _failure_lines(output: str) -> dict[str, str]:
    """Each failing test, keyed without what differs between two runs of it, mapped to its line.

    The key drops numbers (timings, counts) and pytest's reason: a test that failed before the
    change for one reason and after it for another was failing anyway. Seaborn's boxen tests
    failed on a missing KDE method, then on a missing ECDF one, and held back a correct change.
    """
    lines = (line.strip() for line in output.splitlines() if _FAILURE_LINE.search(line))
    return {_NUMBER.sub("#", _PYTEST_REASON.sub(r"\1", _TIMING.sub("", line))): line for line in lines}


async def _checkout(path: str, revision: str) -> bool:
    """Put the copy on *revision*. The agent's work is committed on its branch, so only test leftovers are dropped."""
    process = await asyncio.create_subprocess_exec(
        *("git", "-C", path, "checkout", "-q", "-f", revision),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    return await process.wait() == 0


def _link_dependencies(tree: WorkTree) -> list[Path]:
    """Link the repository's installed dependencies into the copy, which has none, for the tests' sake."""
    links = []
    for name in LINKED_DEPENDENCY_DIRS:
        source, target = Path(tree.base) / name, Path(tree.path) / name
        if source.is_dir() and not target.exists():
            os.symlink(source, target)
            links.append(target)
    return links


def _unlink(links: list[Path]) -> None:
    for link in links:
        with contextlib.suppress(OSError):
            link.unlink()
