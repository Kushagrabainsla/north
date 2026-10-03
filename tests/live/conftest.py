"""Shared pieces for the live tests: throwaway repositories and a real gate in front of the real approval layer."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    directory = tmp_path / "repo"
    directory.mkdir()
    (directory / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (directory / "CLAUDE.md").write_text("Use type hints.\n")
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q"], cwd=directory, check=True)
    subprocess.run([*git, "add", "."], cwd=directory, check=True)
    subprocess.run([*git, "commit", "-qm", "init"], cwd=directory, check=True)
    return directory


@pytest.fixture
async def yolo_gate():
    from approval.models import ApprovalDecision  # noqa: F401
    from config.approval_mode import ApprovalMode
    from tests.live.support import start_gate

    gate = await start_gate(ApprovalMode.YOLO)
    yield gate
    await gate.stop()


@pytest.fixture
async def asking_gate():
    """Ask mode, and every card that appears is rejected."""
    from approval.models import ApprovalDecision
    from config.approval_mode import ApprovalMode
    from tests.live.support import start_gate

    gate = await start_gate(ApprovalMode.ASK, answer=ApprovalDecision.REJECTED)
    yield gate
    await gate.stop()


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A tiny project whose own tests north can run: a base repo whose `.venv` runs this suite's Python."""

    directory = tmp_path / "proj"
    (directory / "src").mkdir(parents=True)
    (directory / "pyproject.toml").write_text("[tool.pytest.ini_options]\npythonpath = ['src']\n")
    (directory / "src" / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (directory / "test_calc.py").write_text("from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n")
    (directory / ".gitignore").write_text(".venv/\n__pycache__/\n.pytest_cache/\n")
    (directory / ".venv" / "bin").mkdir(parents=True)
    python = directory / ".venv" / "bin" / "python"
    python.write_text(f'#!/bin/sh\nexec {sys.executable} "$@"\n')  # a symlink would look like a bare virtualenv
    python.chmod(0o755)
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q"], cwd=directory, check=True)
    subprocess.run([*git, "add", "."], cwd=directory, check=True)
    subprocess.run([*git, "commit", "-qm", "init"], cwd=directory, check=True)
    return directory
