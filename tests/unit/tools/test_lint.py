"""Unit tests for the lint tool (tools/analysis/lint.py).

Command construction is tested by monkeypatching the subprocess runner, so the
tests do not depend on ruff/eslint/gofmt being installed. Parsers and skip paths
are tested directly.
"""

from __future__ import annotations

from pathlib import Path

import tools.analysis.lint as lint_mod
from tools.analysis.lint import LintTool, _parse_eslint_json
from tools.models import ToolInput


async def test_missing_path_is_error() -> None:
    out = await LintTool().run(ToolInput(params={}))
    assert out.success is False
    assert "path" in (out.error or "").lower()


async def test_unsupported_suffix_skips(tmp_path: Path) -> None:
    f = tmp_path / "notes.md"
    f.write_text("# hi\n")
    out = await LintTool().run(ToolInput(params={"path": str(f)}))
    assert out.success is True
    assert out.data["skipped"] is True


async def test_python_without_ruff_skips(tmp_path: Path, monkeypatch) -> None:
    f = tmp_path / "a.py"
    f.write_text("x = 1\n")
    monkeypatch.setattr(lint_mod.shutil, "which", lambda _: None)  # no ruff anywhere
    out = await LintTool().run(ToolInput(params={"path": str(f)}))
    assert out.success is True
    assert out.data["skipped"] is True
    assert "ruff" in out.data["reason"]


async def test_python_reports_issues_from_concise_output(tmp_path: Path, monkeypatch) -> None:
    f = tmp_path / "a.py"
    f.write_text("import os\n")
    monkeypatch.setattr(lint_mod.shutil, "which", lambda name: f"/usr/bin/{name}")

    calls: list[list[str]] = []

    def fake_run(cmd, cwd):
        calls.append(cmd)
        if "check" in cmd:
            return ("a.py:1:8: F401 [*] `os` imported but unused", 1, None)
        return ("", 0, None)  # format --check clean

    monkeypatch.setattr(lint_mod, "_run", fake_run)
    out = await LintTool().run(ToolInput(params={"path": str(f)}))

    assert out.success is False
    assert any("F401" in i for i in out.data["issues"])
    # Used concise output format and did not pass --fix in non-fix mode.
    assert any("--output-format=concise" in c for c in calls)
    assert not any("--fix" in c for c in calls)


async def test_python_fix_mode_runs_fix_and_format(tmp_path: Path, monkeypatch) -> None:
    f = tmp_path / "a.py"
    f.write_text("x=1\n")
    monkeypatch.setattr(lint_mod.shutil, "which", lambda name: f"/usr/bin/{name}")

    calls: list[list[str]] = []

    def fake_run(cmd, cwd):
        calls.append(cmd)
        return ("", 0, None)

    monkeypatch.setattr(lint_mod, "_run", fake_run)
    out = await LintTool().run(ToolInput(params={"path": str(f), "fix": True}))

    assert out.success is True
    assert out.data["fixed"] is True
    assert any("--fix" in c for c in calls)
    assert any(c[:2] == ["ruff", "format"] and "--check" not in c for c in calls)


async def test_python_flags_unformatted_file(tmp_path: Path, monkeypatch) -> None:
    f = tmp_path / "a.py"
    f.write_text("x=1\n")
    monkeypatch.setattr(lint_mod.shutil, "which", lambda name: f"/usr/bin/{name}")

    def fake_run(cmd, cwd):
        if "format" in cmd and "--check" in cmd:
            return ("Would reformat: a.py", 1, None)
        return ("", 0, None)

    monkeypatch.setattr(lint_mod, "_run", fake_run)
    out = await LintTool().run(ToolInput(params={"path": str(f)}))
    assert out.success is False
    assert any("not formatted" in i for i in out.data["issues"])


def test_parse_eslint_json_extracts_issues() -> None:
    payload = (
        '[{"filePath":"/p/a.js","messages":['
        '{"ruleId":"no-unused-vars","severity":2,"message":"x is unused","line":3,"column":5}]}]'
    )
    issues = _parse_eslint_json(payload)
    assert issues == ["/p/a.js:3:5: x is unused (no-unused-vars)"]


def test_parse_eslint_json_clean_is_empty_list() -> None:
    assert _parse_eslint_json('[{"filePath":"/p/a.js","messages":[]}]') == []


def test_parse_eslint_json_bad_output_returns_none() -> None:
    assert _parse_eslint_json("not json at all") is None


def test_format_output_summarizes() -> None:
    t = LintTool()
    assert "clean" in t.format_output({"issues": [], "fixed": False})
    assert "2 issue" in t.format_output({"issues": ["a", "b"], "fixed": False})
    assert "skipped" in t.format_output({"skipped": True, "reason": "linting skipped"}).lower()
