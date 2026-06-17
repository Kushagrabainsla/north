"""Tests for project-aware check_types behaviour (review findings R5#28, R5#29, R5#34)."""

from __future__ import annotations

from pathlib import Path

from tools.analysis import check_types as ct
from tools.analysis.check_types import CheckTypesTool, _find_upward, _resolve_tsc
from tools.models import ToolInput


async def test_unsupported_suffix_returns_neutral_skip(tmp_path: Path) -> None:
    """A file no checker covers must NOT fail - the coder agent halts on failures."""
    file = tmp_path / "notes.txt"
    file.write_text("hello\n", encoding="utf-8")

    result = await CheckTypesTool().run(ToolInput(params={"path": str(file)}))
    assert result.success is True
    assert result.data["skipped"] is True
    assert "skipping" in result.data["reason"].lower()


async def test_missing_file_is_an_error(tmp_path: Path) -> None:
    result = await CheckTypesTool().run(ToolInput(params={"path": str(tmp_path / "nope.py")}))
    assert result.success is False


def test_dead_run_command_helper_was_removed() -> None:
    assert not hasattr(ct, "_run_command")


def test_find_upward_locates_tsconfig(tmp_path: Path) -> None:
    (tmp_path / "tsconfig.json").write_text("{}", encoding="utf-8")
    nested = tmp_path / "src" / "components"
    nested.mkdir(parents=True)
    file = nested / "app.tsx"
    file.write_text("export {}\n", encoding="utf-8")

    assert _find_upward(file, "tsconfig.json") == tmp_path / "tsconfig.json"


def test_find_upward_respects_stop(tmp_path: Path) -> None:
    nested = tmp_path / "pkg"
    nested.mkdir()
    file = nested / "x.ts"
    file.write_text("export {}\n", encoding="utf-8")
    assert _find_upward(file, "tsconfig.json", stop=tmp_path) is None


def test_resolve_tsc_prefers_local_binary(tmp_path: Path) -> None:
    local = tmp_path / "node_modules" / ".bin"
    local.mkdir(parents=True)
    (local / "tsc").write_text("#!/bin/sh\n", encoding="utf-8")
    assert _resolve_tsc(tmp_path) == [str(local / "tsc")]


def test_resolve_tsc_never_auto_installs(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(ct.shutil, "which", lambda name: "/usr/bin/npx" if name == "npx" else None)
    resolved = _resolve_tsc(tmp_path)
    assert resolved == ["npx", "--no-install", "tsc"]


async def test_go_without_module_root_skips(tmp_path: Path) -> None:
    file = tmp_path / "main.go"
    file.write_text("package main\n", encoding="utf-8")
    result = await CheckTypesTool().run(ToolInput(params={"path": str(file)}))
    assert result.success is True
    assert result.data["skipped"] is True


def test_go_command_runs_from_module_root(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "go.mod").write_text("module example.com/m\n", encoding="utf-8")
    pkg = tmp_path / "internal" / "svc"
    pkg.mkdir(parents=True)
    file = pkg / "svc.go"
    file.write_text("package svc\n", encoding="utf-8")

    captured: dict = {}

    def fake_run_checker(cmd, cwd):
        captured["cmd"] = cmd
        captured["cwd"] = cwd
        return "", None

    monkeypatch.setattr(ct, "_run_checker", fake_run_checker)
    monkeypatch.setattr(ct.shutil, "which", lambda _: "/usr/bin/go")

    result = ct._check_go(file)
    assert result.success is True
    assert captured["cwd"] == tmp_path
    assert captured["cmd"] == ["go", "vet", "./internal/svc"]


def test_python_runs_from_project_root_with_relative_file(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    pkg = tmp_path / "src" / "pkg"
    pkg.mkdir(parents=True)
    file = pkg / "mod.py"
    file.write_text("x: int = 1\n", encoding="utf-8")

    captured: dict = {}

    def fake_run_checker(cmd, cwd):
        captured["cmd"] = cmd
        captured["cwd"] = cwd
        return "", None

    monkeypatch.setattr(ct, "_run_checker", fake_run_checker)

    result = ct._check_python(file)
    assert result.success is True
    assert captured["cwd"] == tmp_path
    assert captured["cmd"][-1] == "src/pkg/mod.py"
    assert captured["cmd"][1:3] == ["-m", "mypy"]


def test_typescript_uses_project_mode_when_tsconfig_exists(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "tsconfig.json").write_text("{}", encoding="utf-8")
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    file = tmp_path / "index.ts"
    file.write_text("export {}\n", encoding="utf-8")

    captured: dict = {}

    def fake_run_checker(cmd, cwd):
        captured["cmd"] = cmd
        captured["cwd"] = cwd
        return "", None

    monkeypatch.setattr(ct, "_run_checker", fake_run_checker)
    monkeypatch.setattr(ct, "_resolve_tsc", lambda root: ["tsc"])

    result = ct._check_typescript(file)
    assert result.success is True
    assert captured["cmd"] == ["tsc", "--noEmit", "-p", str(tmp_path / "tsconfig.json")]
    assert captured["cwd"] == tmp_path
