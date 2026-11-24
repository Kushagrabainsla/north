"""The nightly smoke script runs each test once, in its own stage."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
# Stands in for the venv's python and ruff: it writes down how it was called.
_RECORDER = '#!/usr/bin/env bash\necho "$(basename "$0") $*" >> "$(dirname "$0")/../../calls"\n'


@pytest.fixture
def copy(tmp_path: Path) -> Path:
    (tmp_path / "scripts").mkdir()
    shutil.copy(_REPO / "scripts" / "smoke.sh", tmp_path / "scripts" / "smoke.sh")
    bin_folder = tmp_path / ".venv" / "bin"
    bin_folder.mkdir(parents=True)
    for name in ("python", "ruff"):
        (bin_folder / name).write_text(_RECORDER, encoding="utf-8")
        (bin_folder / name).chmod(0o755)
    return tmp_path


def test_with_the_live_switches_on_the_live_tests_run_only_in_their_own_stage(copy: Path) -> None:
    env = {
        **os.environ,
        "SMOKE_LIVE": "1",
        "NORTH_LIVE_CLAUDE": "1",
        "NORTH_LIVE_CODEX": "1",
        "NORTH_HOME": str(copy / "home"),
        "PATH": "/usr/bin:/bin",
    }

    subprocess.run(["bash", str(copy / "scripts" / "smoke.sh")], env=env, check=True, capture_output=True)

    pytest_calls = [line for line in (copy / "calls").read_text().splitlines() if " pytest " in line]
    suite, live = pytest_calls
    assert "--ignore=tests/live" in suite.split()
    assert "tests/live" in live.split()
