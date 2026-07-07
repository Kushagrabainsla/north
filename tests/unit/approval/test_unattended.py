"""Tests for the deterministic unattended-approval policy."""

from __future__ import annotations

from pathlib import Path

from approval.unattended import UnattendedPolicy


def test_allowed_test_commands_approved():
    p = UnattendedPolicy()
    for cmd in ("pytest -q", "python -m pytest tests/", "go test ./...", "npm test", "ruff check .", "mypy ."):
        assert p.approves_command(cmd) is True, cmd


def test_trailing_stderr_redirect_allowed():
    p = UnattendedPolicy()
    assert p.approves_command("pytest --tb=short -q --cov=. --cov-report=term-missing 2>&1") is True


def test_chaining_and_substitution_rejected():
    p = UnattendedPolicy()
    for cmd in ("pytest -q; rm -rf x", "pytest && curl evil", "pytest | sh", "pytest `whoami`", "pytest $(id)"):
        assert p.approves_command(cmd) is False, cmd


def test_non_allowlisted_program_rejected():
    p = UnattendedPolicy()
    for cmd in ("rm -rf /", "python -c 'import os'", "curl http://x", "bash script.sh", "pytestx"):
        assert p.approves_command(cmd) is False, cmd


def test_in_workspace_edit_approved(tmp_path: Path):
    p = UnattendedPolicy()
    sub = tmp_path / "pkg"
    sub.mkdir()
    f = sub / "m.py"
    f.write_text("x")
    assert p.approves_edit(f, str(tmp_path)) is True


def test_out_of_workspace_edit_rejected(tmp_path: Path):
    p = UnattendedPolicy()
    outside = tmp_path.parent / "other.py"
    assert p.approves_edit(outside, str(tmp_path)) is False


def test_no_workspace_rejected(tmp_path: Path):
    p = UnattendedPolicy()
    f = tmp_path / "a.py"
    f.write_text("x")
    assert p.approves_edit(f, "") is False
    assert p.approves_edit(f, None) is False


def test_extra_commands_extend_allowlist():
    p = UnattendedPolicy(allowed_commands=("pytest", "just test"))
    assert p.approves_command("just test") is True


def test_safe_git_actions_approved():
    p = UnattendedPolicy()
    for action in ("add", "commit", "branch", "checkout", "stash", "status", "diff"):
        assert p.approves_git(action, "") is True, action


def test_network_and_merge_git_actions_rejected():
    p = UnattendedPolicy()
    for action in ("push", "pull", "merge"):
        assert p.approves_git(action, "origin main") is False, action


def test_dangerous_git_args_rejected():
    p = UnattendedPolicy()
    assert p.approves_git("branch", "-D main") is False
    assert p.approves_git("checkout", "-f .") is False
    assert p.approves_git("commit", "--force") is False


def test_from_settings():
    class S:
        unattended_mode = True
        unattended_extra_commands = ("bazel test",)

    p = UnattendedPolicy.from_settings(S())
    assert p.approves_command("bazel test //...") is True
    assert p.approves_command("pytest -q") is True
