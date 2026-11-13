"""The environment a coding agent starts with."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from coding_agents.constants import INHERITED_ENVIRONMENT, NETWORK_ENVIRONMENT

# Where a project keeps its virtualenv, by the two names tools make it with.
_VENV_NAMES = (".venv", "venv")


def agent_environment(source: Mapping[str, str] | None = None, *, network: bool = False) -> dict[str, str]:
    """*source* narrowed to what a worker needs; north's secrets and other providers' keys stay behind.

    The one place a child process's environment is read: the process boundary, not
    a setting. *network* adds the proxy and certificate settings a child that calls
    a web API needs, such as an MCP server.
    """
    current = os.environ if source is None else source
    names = INHERITED_ENVIRONMENT + (NETWORK_ENVIRONMENT if network else ())
    return {name: current[name] for name in names if name in current}


def in_project(environment: Mapping[str, str], workspace: str) -> dict[str, str]:
    """*environment* with the project's virtualenv active, as a terminal opened in the project has it.

    A copy carries the project's virtualenv, yet an agent ran the system `python` beside it, found
    no jinja2 and could not run one test (benchmark, 2026-11-11). Activated, plain `python` and
    `pytest` are the project's. A project without one starts as it did.
    """
    venv = _virtualenv_of(Path(workspace))
    if venv is None:
        return dict(environment)
    path = environment.get("PATH")
    scripts = str(venv / "bin")
    return {**environment, "VIRTUAL_ENV": str(venv), "PATH": f"{scripts}{os.pathsep}{path}" if path else scripts}


def _virtualenv_of(workspace: Path) -> Path | None:
    """The virtualenv in *workspace* or a folder above it, stopping at the repository's top."""
    for folder in (workspace, *workspace.parents):
        for name in _VENV_NAMES:
            if (folder / name / "pyvenv.cfg").is_file():
                return folder / name
        if (folder / ".git").exists():
            return None
    return None
