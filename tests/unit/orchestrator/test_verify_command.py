"""Tests for the orchestrator's safe verification-command detection (B2).

`_detect_verify_command` must only ever return a FIXED literal command derived
from a project marker - never a string taken from repo content - and must return
None for unknown project layouts so the DoD stays fail-open.
"""

from __future__ import annotations

from pathlib import Path

from orchestrator.orchestrator import _detect_verify_command


def test_detects_pytest_from_pytest_ini(tmp_path: Path) -> None:
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    # No local venv -> bare pytest (a missing runner later fails open via exit 127).
    assert _detect_verify_command(str(tmp_path)) == "pytest -q"


def test_detects_pytest_from_pyproject_marker(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\naddopts = '-q'\n", encoding="utf-8")
    assert _detect_verify_command(str(tmp_path)) == "pytest -q"


def test_pytest_uses_project_venv_when_present(tmp_path: Path) -> None:
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    venv_python = tmp_path / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("", encoding="utf-8")
    cmd = _detect_verify_command(str(tmp_path))
    assert cmd is not None and cmd.endswith("-m pytest -q")
    assert str(venv_python) in cmd  # runs through the project's own interpreter


def test_pyproject_without_pytest_marker_is_not_detected(tmp_path: Path) -> None:
    # A pyproject with no pytest section must NOT trigger a pytest run (avoids false
    # negatives on projects that don't use pytest).
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
    assert _detect_verify_command(str(tmp_path)) is None


def test_detects_go(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("module example.com/x\n", encoding="utf-8")
    assert _detect_verify_command(str(tmp_path)) == "go test ./..."


def test_detects_cargo(tmp_path: Path) -> None:
    (tmp_path / "Cargo.toml").write_text("[package]\nname = 'x'\n", encoding="utf-8")
    assert _detect_verify_command(str(tmp_path)) == "cargo test"


def test_unknown_project_returns_none(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("hi\n", encoding="utf-8")
    assert _detect_verify_command(str(tmp_path)) is None


def test_missing_workspace_returns_none() -> None:
    assert _detect_verify_command("") is None
    assert _detect_verify_command("/nonexistent/path/xyz") is None


def test_detection_is_a_fixed_literal_never_repo_content(tmp_path: Path) -> None:
    # Even if the marker file is stuffed with a malicious "command", detection must
    # return only the fixed literal - never anything read from the file body.
    (tmp_path / "pytest.ini") .write_text("[pytest]\n; rm -rf / #\n", encoding="utf-8")
    assert _detect_verify_command(str(tmp_path)) == "pytest -q"
