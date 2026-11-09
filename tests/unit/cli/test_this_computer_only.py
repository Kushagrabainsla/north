"""North listens on this computer only (CODING_STYLE §12.6, issue #38).

`north start --host 0.0.0.0` put north's API on every network the computer was on, guarded by one
shared secret over plain HTTP: anyone on the same Wi-Fi could reach it. OpenClaw's 2026 incidents
came from that shape. Until TLS and per-client tokens exist (issue #40), north refuses.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from cli.main import app
from config.server_url import is_this_computer

runner = CliRunner()


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "localhost", "LOCALHOST", "::1", "[::1]"])
def test_an_address_on_this_computer_is_one(host: str) -> None:
    assert is_this_computer(host)


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.20", "10.0.0.5", "fe80::1", "my-mac.local", ""])
def test_any_other_address_is_not(host: str) -> None:
    assert not is_this_computer(host)


@pytest.mark.parametrize("command", [["start", "--no-chat"], ["web", "--no-open"], ["start", "--docker"]])
def test_north_refuses_to_listen_beyond_this_computer_before_it_starts_anything(command, monkeypatch) -> None:
    started: list[str] = []
    for step in ("_start_locally", "_start_with_docker", "_docker_available", "_ensure_web_build", "pairing_url"):
        monkeypatch.setattr(f"cli.main.{step}", lambda *a, _step=step, **k: started.append(_step))

    result = runner.invoke(app, [*command, "--host", "0.0.0.0"])

    assert result.exit_code == 1 and started == []
    said = " ".join(result.output.split()).lower()
    assert "will not listen on 0.0.0.0" in said and "anyone on your network" in said and "127.0.0.1" in said


def test_this_computer_still_starts(monkeypatch) -> None:
    started: list[str] = []
    monkeypatch.setattr("cli.main._start_locally", lambda options: started.append(options.host))

    result = runner.invoke(app, ["start", "--no-chat", "--host", "localhost"])

    assert result.exit_code == 0 and started == ["localhost"]
