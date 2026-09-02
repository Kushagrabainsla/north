"""Detecting and running a project's own verification command.

The orchestrator uses this as an executable oracle for the Definition of Done:
rather than trust an agent's claim that the tests pass, it runs them. Only
well-known runners are used, and every command string is a literal - never built
from repo content - so there is no shell-injection surface.
"""

from __future__ import annotations

import shlex
from pathlib import Path

_VERIFY_COMMAND_TIMEOUT: int = 300

_AUTO_VERIFY_RULES: tuple[tuple[str, str | None, str], ...] = (
    ("pytest.ini", None, "pytest -q"),
    ("pyproject.toml", "[tool.pytest", "pytest -q"),
    ("setup.cfg", "[tool:pytest]", "pytest -q"),
    ("go.mod", None, "go test ./..."),
    ("Cargo.toml", None, "cargo test"),
)

def _project_venv_python(root: Path) -> str | None:
    """Quoted path to a project-local virtualenv's Python, if one exists, else None.

    Running the venv's own interpreter (`.venv/bin/python -m pytest`) uses the deps
    installed there. When there is no local venv we fall back to a bare `pytest`,
    which - if it isn't on PATH either - exits 127 and is treated as "couldn't run"
    (fail-open), never as a test failure.
    """
    for rel in (".venv/bin/python", "venv/bin/python", ".venv/Scripts/python.exe"):
        candidate = root / rel
        if candidate.is_file():
            return shlex.quote(str(candidate))
    return None

def _detect_verify_command(workspace: str) -> str | None:
    """Detect a SAFE, fixed test command from project markers, or None.

    Only well-known runners whose command string is a literal (never taken from
    repo content) are used, so there is no shell-injection surface and unusual
    project setups simply yield None (skip) rather than a spurious failure. A
    pytest command is offered only when a real pytest marker is present, and runs
    through the project's own venv interpreter when one exists.
    """
    try:
        root = Path(workspace).expanduser()
        if not workspace or not root.is_dir():
            return None
    except OSError:
        return None
    for marker, needle, command in _AUTO_VERIFY_RULES:
        path = root / marker
        if not path.is_file():
            continue
        if needle is not None:
            try:
                if needle not in path.read_text(encoding="utf-8", errors="ignore"):
                    continue
            except OSError:
                continue
        if command.startswith("pytest"):
            venv_python = _project_venv_python(root)
            return f"{venv_python} -m pytest -q" if venv_python else "pytest -q"
        return command
    return None
