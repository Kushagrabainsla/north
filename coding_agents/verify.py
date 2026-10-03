"""Running the project's own tests on an agent's changes, ourselves.

An agent says its work passes; north does not take its word. The command comes from the project's own
markers (never from what the agent wrote), runs in the agent's copy through `Shell` (so it is ruled on and
sandboxed like any command north runs), and the result is what north reports.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Callable
from pathlib import Path

from coding_agents.base import Shell
from coding_agents.constants import LINKED_DEPENDENCY_DIRS, MAX_VERIFY_OUTPUT_CHARS, VERIFY_TIMEOUT_SECONDS
from coding_agents.models import Verification, VerificationState, WorkTree

# What a shell prints when the program it was asked to run is not there.
_COULD_NOT_RUN = frozenset({126, 127})


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
        finally:
            _unlink(links)
        if result.refused:
            return Verification(VerificationState.DECLINED, command, "you did not let the tests run")
        if result.exit_code is None:
            return Verification(VerificationState.SKIPPED, command, f"it could not be run: {result.error}".strip())
        if result.exit_code in _COULD_NOT_RUN:
            return Verification(VerificationState.SKIPPED, command, "its test runner is not installed")
        if result.exit_code == 0:
            return Verification(VerificationState.PASSED, command)
        return Verification(VerificationState.FAILED, command, result.output.strip()[-MAX_VERIFY_OUTPUT_CHARS:])


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
