"""Unit tests for ShellTool internals."""

from __future__ import annotations

import asyncio
import os

import pytest

from tools.specialized import shell_tool


@pytest.mark.skipif(not hasattr(os, "openpty"), reason="pty requires a Unix platform")
def test_spawn_session_closes_both_fds_on_spawn_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the subprocess fails to spawn, both pty fds are closed - no leak (B6)."""
    opened: dict[str, int] = {}
    real_openpty = shell_tool.pty.openpty

    def fake_openpty() -> tuple[int, int]:
        master, slave = real_openpty()
        opened["master"], opened["slave"] = master, slave
        return master, slave

    closed: list[int] = []
    real_close = os.close

    def spy_close(fd: int) -> None:
        closed.append(fd)
        real_close(fd)

    async def fail_spawn(*_args: object, **_kwargs: object) -> object:
        raise OSError("cannot spawn")

    monkeypatch.setattr(shell_tool.pty, "openpty", fake_openpty)
    monkeypatch.setattr(shell_tool.os, "close", spy_close)
    monkeypatch.setattr(shell_tool.asyncio, "create_subprocess_shell", fail_spawn)

    with pytest.raises(OSError, match="cannot spawn"):
        asyncio.run(shell_tool._spawn_session("false", None))

    assert opened["master"] in closed, "master pty fd leaked on spawn failure"
    assert opened["slave"] in closed, "slave pty fd leaked on spawn failure"
