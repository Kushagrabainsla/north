"""Committing the coder's work without spending model turns on it.

Branching, staging and committing need no judgement, and each one cost a full
LLM round-trip: measured on a two-line fix, six of the coder's twelve turns were
version control, about a third of its 142 seconds. The orchestrator does it now,
reading what actually changed on disk rather than asking the model to recall
which files it touched.
"""

from __future__ import annotations

import pytest

from orchestrator.commit import WorkCommitter, changed_paths
from tools.models import ToolOutput


class TestReadingWhatChanged:
    def test_modified_and_untracked_files_are_both_staged(self) -> None:
        """A new test file is part of the change, not noise."""
        status = "## north/task_1\n M src/cart.py\n?? tests/test_new.py\n"

        assert changed_paths(status) == ["src/cart.py", "tests/test_new.py"]

    def test_generated_directories_are_never_staged(self) -> None:
        """The coder prompt's "never `git add .`" rule existed to stop exactly
        this. In a repo with no .gitignore, `__pycache__` shows up as untracked
        and would otherwise land in the commit."""
        status = (
            "## main\n M src/cart.py\n?? src/__pycache__/\n?? .venv/lib/x.py\n"
            "?? node_modules/pkg/index.js\n?? build/out.o\n"
        )

        assert changed_paths(status) == ["src/cart.py"]

    def test_the_branch_header_is_not_a_path(self) -> None:
        assert changed_paths("## main...origin/main [ahead 1]\n") == []

    def test_a_rename_stages_the_new_path(self) -> None:
        status = "## main\nR  src/old.py -> src/new.py\n"

        assert changed_paths(status) == ["src/new.py"]

    def test_a_deletion_is_staged_so_the_removal_is_recorded(self) -> None:
        assert changed_paths("## main\n D src/gone.py\n") == ["src/gone.py"]

    def test_quoted_paths_are_unwrapped(self) -> None:
        """git quotes paths containing spaces or unusual characters."""
        assert changed_paths('## main\n M "src/my file.py"\n') == ["src/my file.py"]

    def test_a_clean_tree_stages_nothing(self) -> None:
        assert changed_paths("## main\n") == []


class _FakeGit:
    """Records the git actions asked for, and answers as configured."""

    def __init__(self, status_out: str, *, fails: set[str] = frozenset()) -> None:
        self.calls: list[tuple[str, str]] = []
        self._status_out = status_out
        self._fails = fails

    async def run(self, tool_input):
        action = tool_input.params["action"]
        args = tool_input.params.get("args", "")
        self.calls.append((action, args))
        if action in self._fails:
            return ToolOutput(success=False, error="refused", failure_kind="refused")
        if action == "status":
            return ToolOutput(success=True, data={"stdout": self._status_out})
        return ToolOutput(success=True, data={"stdout": ""})


async def _commit(git, task_id: str = "task_1") -> str | None:
    return await WorkCommitter(git).commit(
        workspace="/ws", task_id=task_id, message="implement: thing"
    )


class TestCommittingTheWork:
    async def test_it_branches_stages_and_commits_in_one_pass(self) -> None:
        git = _FakeGit("## main\n M src/cart.py\n?? tests/test_new.py\n")

        assert await _commit(git) == "north/task_1"

        actions = [action for action, _ in git.calls]
        assert actions == ["status", "checkout", "add", "commit"]
        assert git.calls[1] == ("checkout", "-b north/task_1")
        # One `add` with explicit paths, not one call per file.
        assert git.calls[2] == ("add", "src/cart.py tests/test_new.py")

    async def test_an_existing_task_branch_is_not_recreated(self) -> None:
        git = _FakeGit("## north/task_1\n M src/cart.py\n")

        assert await _commit(git) == "north/task_1"
        assert [action for action, _ in git.calls] == ["status", "add", "commit"]

    async def test_a_clean_tree_commits_nothing(self) -> None:
        """The coder changed nothing, so there is nothing to record - and an
        empty commit would be a lie about what happened."""
        git = _FakeGit("## main\n")

        assert await _commit(git) is None
        assert [action for action, _ in git.calls] == ["status"]

    async def test_a_refused_commit_does_not_raise(self) -> None:
        """Bookkeeping must never fail a task whose change was applied and
        verified - the work is on disk either way."""
        git = _FakeGit("## main\n M src/cart.py\n", fails={"commit"})

        assert await _commit(git) is None

    @pytest.mark.parametrize("refused", ["status", "add"])
    async def test_any_refused_step_stops_quietly(self, refused: str) -> None:
        git = _FakeGit("## main\n M src/cart.py\n", fails={refused})

        assert await _commit(git) is None

    async def test_a_branch_that_already_exists_is_switched_to(self) -> None:
        """A fix round returns to a branch created in the first one, so `-b`
        fails and a plain checkout is the right recovery."""

        class _BranchExists(_FakeGit):
            async def run(self, tool_input):
                action = tool_input.params["action"]
                args = tool_input.params.get("args", "")
                if action == "checkout" and args.startswith("-b"):
                    self.calls.append((action, args))
                    return ToolOutput(success=False, error="already exists")
                return await super().run(tool_input)

        git = _BranchExists("## main\n M src/cart.py\n")

        assert await _commit(git) == "north/task_1"
        assert ("checkout", "north/task_1") in git.calls
        assert [action for action, _ in git.calls][-1] == "commit"
