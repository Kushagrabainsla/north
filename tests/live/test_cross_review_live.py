"""One real agent reads the other's change. Skipped unless both are installed and opted in.

Proves what the fakes cannot: that a real reviewer, handed only a diff, gives the verdict line north parses,
passes a correct change, and flags a plainly wrong one.

NORTH_LIVE_CLAUDE=1 NORTH_LIVE_CODEX=1 .venv/bin/python -m pytest tests/live/test_cross_review_live.py -q
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from coding_agents import ClaudeBackend, CodexBackend, CodingRunner, ReviewVerdict
from tests.live.helpers import status, workspaces
from tests.unit.coding_agents.conftest import MemoryRecorder

pytestmark = pytest.mark.skipif(
    os.environ.get("NORTH_LIVE_CLAUDE") != "1"
    or os.environ.get("NORTH_LIVE_CODEX") != "1"
    or shutil.which("claude") is None
    or shutil.which("codex") is None,
    reason="set NORTH_LIVE_CLAUDE=1 and NORTH_LIVE_CODEX=1 with both CLIs installed",
)

TASK = "Add a subtract(a, b) function to calc.py that returns a minus b."
CORRECT = "def add(a, b):\n    return a + b\n\n\ndef subtract(a, b):\n    return a - b\n"
WRONG = "def add(a, b):\n    return a + b\n\n\ndef subtract(a, b):\n    return a + b\n"


async def _review(repo: Path, tmp_path: Path, code: str, author: str):
    spaces = workspaces()
    runner = CodingRunner(
        {"claude": ClaudeBackend(protected_paths=[str(tmp_path / "north_home")]), "codex": CodexBackend()},
        MemoryRecorder(),
        workspaces=spaces,
    )
    tree = await spaces.create(str(repo), "cross-review-live")
    Path(tree.path, "calc.py").write_text(code)
    change = await spaces.finish(tree)
    assert change is not None
    review = await runner._review(change, TASK, "t-review", author=author)
    assert review is not None
    return review, change


@pytest.mark.parametrize("author", ["claude", "codex"])
async def test_a_correct_change_is_not_flagged_by_the_other_agent(repo, tmp_path, author) -> None:
    review, change = await _review(repo, tmp_path, CORRECT, author)

    assert review.reviewer != author
    assert review.verdict is ReviewVerdict.OK, review.summary
    assert status(change.tree.path) == "", "reviewing must not touch the copy"


@pytest.mark.parametrize("author", ["claude", "codex"])
async def test_a_plainly_wrong_change_is_flagged_by_the_other_agent(repo, tmp_path, author) -> None:
    review, _ = await _review(repo, tmp_path, WRONG, author)

    assert review.reviewer != author
    assert review.verdict is ReviewVerdict.CONCERNS, review.summary
    assert "subtract" in review.summary.lower()
