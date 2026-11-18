"""`cd` into a folder of north's home that a command may read works (issue #82, a small item).

`/bin/sh` is bash in POSIX mode, whose `cd` checks every parent of the path. North's
home is closed to commands, so `cd ~/.north/tasks/<id>` failed "Not a directory"
while `ls` of the same folder worked, and a run read it as the folder missing.
"""

from __future__ import annotations

import subprocess

import pytest

from tools.specialized import _seatbelt

pytestmark = pytest.mark.skipif(not _seatbelt.available(), reason="needs macOS Seatbelt")


@pytest.fixture
def north_home(tmp_path, move_north_home):
    home = move_north_home(tmp_path / "north")
    (home / "tasks" / "t1").mkdir(parents=True)
    (home / "notes").mkdir()
    (home / "facts.db").write_text("private")
    return home


def _run(command: str, workspace: str | None = None) -> subprocess.CompletedProcess:
    argv = _seatbelt.wrap(command, workspace, writable=False)
    return subprocess.run(argv, capture_output=True, text=True, timeout=20)


@pytest.mark.parametrize("folder", ["tasks", "tasks/t1", "notes"])
def test_cd_into_an_open_folder_of_norths_home_works(north_home, folder) -> None:
    done = _run(f"cd {north_home / folder} && pwd")

    assert done.returncode == 0, done.stderr
    assert done.stdout.strip().endswith(folder)


def test_norths_home_itself_stays_closed(north_home) -> None:
    assert _run(f"ls {north_home}").returncode != 0
    assert _run(f"cat {north_home / 'facts.db'}").returncode != 0
