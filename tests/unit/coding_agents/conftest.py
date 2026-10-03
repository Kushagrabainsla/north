"""Shared pieces for the coding-agent tests: a fake `claude` and a recorder that keeps what it is told."""

from __future__ import annotations

import json
import shutil
import stat
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

from coding_agents import LiveRun, RunOutcome, RunStart, WorkChange, WorkTree

HERE = Path(__file__).parent
FIXTURES = HERE / "fixtures"


@pytest.fixture
def make_fake_claude(tmp_path: Path) -> Callable[..., Path]:
    """Build a runnable fake `claude` in tmp_path, configured by keyword."""

    def build(**config: Any) -> Path:
        script = tmp_path / "claude"
        shutil.copy(HERE / "fake_claude.py", script)
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        (tmp_path / "claude.json").write_text(json.dumps({"fixtures_dir": str(FIXTURES), **config}))
        return script

    return build


@pytest.fixture
def make_fake_codex(tmp_path: Path) -> Callable[..., Path]:
    """Build a runnable fake `codex` in tmp_path, configured by keyword."""

    def build(**config: Any) -> Path:
        script = tmp_path / "codex"
        shutil.copy(HERE / "fake_codex.py", script)
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        (tmp_path / "codex.json").write_text(json.dumps({"fixtures_dir": str(FIXTURES), **config}))
        return script

    return build


class MemoryRecorder:
    """A `RunRecorder` that keeps everything in lists, for asserting on."""

    def __init__(self, live: LiveRun | None = None) -> None:
        self.live = live
        self.live_asked: tuple[str, str, str] | None = None
        self.calls: list[str] = []
        self.started: list[RunStart] = []
        self.state: dict[str, dict[str, Any]] = {}
        self.events: list[tuple[str, Mapping[str, Any]]] = []
        self.finished: list[tuple[str, RunOutcome]] = []

    async def live_run(self, task_id: str, agent: str, mode: str) -> LiveRun | None:
        self.live_asked = (task_id, agent, mode)
        return self.live

    async def start(self, run: RunStart) -> None:
        self.calls.append("start")
        self.started.append(run)

    async def remember(self, run_id: str, state: Mapping[str, Any]) -> None:
        self.calls.append("remember")
        self.state.setdefault(run_id, {}).update(state)

    async def record(self, run_id: str, task_id: str, event: str, data: Mapping[str, Any]) -> None:
        self.calls.append("record")
        self.events.append((event, data))

    async def finish(self, run_id: str, outcome: RunOutcome) -> None:
        self.calls.append("finish")
        self.finished.append((run_id, outcome))


@pytest.fixture
def recorder() -> MemoryRecorder:
    return MemoryRecorder()


class MemoryWorkspaces:
    """A `Workspaces` that makes a real directory per copy and reports a scripted change."""

    def __init__(self, root: Path, change_files: tuple = ()) -> None:
        self._root = root
        self.created: list[WorkTree] = []
        self.finished: list[WorkTree] = []
        self.change_files = change_files

    async def create(self, workspace: str, label: str) -> WorkTree:
        path = self._root / label
        path.mkdir(parents=True)
        tree = WorkTree(str(path), f"north/wt-{label}", "base123", workspace)
        self.created.append(tree)
        return tree

    async def finish(self, tree: WorkTree) -> WorkChange | None:
        self.finished.append(tree)
        return WorkChange(tree, self.change_files) if self.change_files else None
