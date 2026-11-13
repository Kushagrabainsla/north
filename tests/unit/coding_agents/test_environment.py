"""A coding agent starts with the project's virtualenv active, as a terminal in the project would."""

from __future__ import annotations

import os
from pathlib import Path

from coding_agents.environment import in_project


def _virtualenv(folder: Path, name: str = ".venv") -> Path:
    venv = folder / name
    venv.mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = /usr/bin\n")
    return venv


def test_the_projects_virtualenv_comes_first_on_the_path(tmp_path) -> None:
    venv = _virtualenv(tmp_path)

    env = in_project({"PATH": "/usr/bin:/bin", "HOME": "/h"}, str(tmp_path))

    assert env["VIRTUAL_ENV"] == str(venv)
    assert env["PATH"] == f"{venv / 'bin'}{os.pathsep}/usr/bin:/bin"
    assert env["HOME"] == "/h"


def test_a_subfolder_of_the_project_finds_the_virtualenv_at_its_top(tmp_path) -> None:
    (tmp_path / ".git").mkdir()
    venv = _virtualenv(tmp_path, "venv")
    sub = tmp_path / "lib" / "pkg"
    sub.mkdir(parents=True)

    assert in_project({"PATH": "/bin"}, str(sub))["VIRTUAL_ENV"] == str(venv)


def test_a_virtualenv_above_the_repository_is_not_the_projects(tmp_path) -> None:
    _virtualenv(tmp_path)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)

    assert in_project({"PATH": "/bin"}, str(repo)) == {"PATH": "/bin"}


def test_a_folder_named_venv_that_is_not_a_virtualenv_is_left_alone(tmp_path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".venv").mkdir()

    assert in_project({"PATH": "/bin"}, str(tmp_path)) == {"PATH": "/bin"}
