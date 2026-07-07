"""Tests for the Docker sandbox (#6) - argv construction, availability, fail-closed."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tools.models import ToolInput
from tools.specialized import _sandbox
from tools.specialized._sandbox import SandboxConfig, build_run_argv
from tools.specialized.bash import BashTool


def _approving_store():
    from unittest.mock import AsyncMock, MagicMock

    store = MagicMock()
    resolved = MagicMock()
    resolved.chosen_option = "Approve"
    resolved.status = "approved"
    store.wait_for_decision = AsyncMock(return_value=resolved)
    return store


def test_build_run_argv_mounts_workspace_and_limits(tmp_path: Path):
    cfg = SandboxConfig(enabled=True, image="python:3.12-slim", memory="256m", cpus="2", pids_limit=128)
    argv = build_run_argv("pytest -q", str(tmp_path), cfg)

    assert argv[0:2] == ["docker", "run"]
    assert "--rm" in argv
    assert argv[-3:] == ["sh", "-c", "pytest -q"]
    # workspace bind-mounted at /workspace and used as workdir
    assert f"{tmp_path.resolve()}:/workspace" in argv
    assert argv[argv.index("-w") + 1] == "/workspace"
    # resource + hardening flags present
    assert argv[argv.index("--memory") + 1] == "256m"
    assert argv[argv.index("--cpus") + 1] == "2"
    assert argv[argv.index("--pids-limit") + 1] == "128"
    assert "no-new-privileges" in argv
    assert argv[argv.index("--network") + 1] == "none"


def test_build_run_argv_network_enabled_omits_none(tmp_path: Path):
    cfg = SandboxConfig(enabled=True, network_disabled=False)
    argv = build_run_argv("echo hi", str(tmp_path), cfg)
    assert "--network" not in argv


def test_from_settings_reads_knobs():
    class S:
        sandbox_enabled = True
        sandbox_image = "node:20"
        sandbox_network_disabled = False
        sandbox_memory = "1g"
        sandbox_cpus = "4"
        sandbox_pids_limit = 256

    cfg = SandboxConfig.from_settings(S())
    assert cfg.enabled and cfg.image == "node:20"
    assert cfg.network_disabled is False and cfg.memory == "1g" and cfg.cpus == "4"


@pytest.mark.asyncio
async def test_bash_sandbox_fails_closed_without_docker(tmp_path: Path, monkeypatch):
    async def no_docker():
        return False

    monkeypatch.setattr("tools.specialized.bash.docker_available", no_docker)
    tool = BashTool(approval_store=_approving_store(), sandbox=SandboxConfig(enabled=True))
    out = await tool.run(ToolInput(params={"command": "echo hi", "workspace": str(tmp_path)}))
    assert not out.success
    assert "Docker is unavailable" in (out.error or "")


@pytest.mark.asyncio
async def test_bash_sandbox_requires_workspace(monkeypatch):
    async def yes_docker():
        return True

    monkeypatch.setattr("tools.specialized.bash.docker_available", yes_docker)
    tool = BashTool(approval_store=_approving_store(), sandbox=SandboxConfig(enabled=True))
    out = await tool.run(ToolInput(params={"command": "echo hi"}))
    assert not out.success
    assert "workspace" in (out.error or "").lower()


@pytest.mark.asyncio
async def test_bash_sandbox_runs_docker_argv(tmp_path: Path, monkeypatch):
    async def yes_docker():
        return True

    captured: dict[str, list[str]] = {}

    async def fake_exec(*argv, **kwargs):
        captured["argv"] = list(argv)

        class _P:
            returncode = 0

            async def communicate(self):
                return b"container-stdout", b""

            def kill(self):
                pass

        return _P()

    monkeypatch.setattr("tools.specialized.bash.docker_available", yes_docker)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    tool = BashTool(approval_store=_approving_store(), sandbox=SandboxConfig(enabled=True))
    out = await tool.run(ToolInput(params={"command": "pytest -q", "workspace": str(tmp_path)}))

    assert out.success
    assert out.data["stdout"] == "container-stdout"
    assert captured["argv"][0:2] == ["docker", "run"]
    assert captured["argv"][-3:] == ["sh", "-c", "pytest -q"]


@pytest.mark.asyncio
async def test_bash_without_sandbox_runs_on_host(tmp_path: Path):
    tool = BashTool(approval_store=_approving_store())  # sandbox disabled by default
    out = await tool.run(ToolInput(params={"command": "echo hello-host", "workspace": str(tmp_path)}))
    assert out.success
    assert "hello-host" in out.data["stdout"]


@pytest.mark.asyncio
async def test_docker_available_false_when_binary_missing(monkeypatch):
    async def boom(*a, **k):
        raise FileNotFoundError("no docker")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", boom)
    assert await _sandbox.docker_available() is False
