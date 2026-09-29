"""`north --yolo` and `north status` read and set the mode through the settings API (#28).

`--yolo` used to auto-answer cards inside this terminal only, and `status` read
keys (`strategy`, `approval_mode`) the API does not return, so it always showed
the defaults.
"""

from __future__ import annotations

from typer.testing import CliRunner

import cli.main as main

runner = CliRunner()


class _Reply:
    def __init__(self, payload) -> None:
        self._payload = payload

    def json(self):
        return self._payload


def _run_task_offline(monkeypatch, calls: list) -> None:
    def fake_api(method, path, **kwargs):
        calls.append((method, path, kwargs.get("json")))
        return _Reply({"autonomy": "yolo"})

    monkeypatch.setattr(main, "_api", fake_api)
    monkeypatch.setattr(main, "_submit_task", lambda prompt, workspace: calls.append("submit") or "task_1")
    monkeypatch.setattr(main, "follow_task", lambda stream: (_ for _ in ()).throw(KeyboardInterrupt))


def test_yolo_switches_north_to_yolo_before_the_task_is_sent(monkeypatch) -> None:
    calls: list = []
    _run_task_offline(monkeypatch, calls)

    result = runner.invoke(main.app, ["--yolo", "task", "tidy up"])

    assert result.exit_code == 0, result.output
    assert calls[0] == ("POST", "/orchestrator/settings", {"autonomy": "yolo"})
    assert calls[1] == "submit"
    assert "YOLO" in result.output


def test_without_yolo_the_mode_is_left_alone(monkeypatch) -> None:
    calls: list = []
    _run_task_offline(monkeypatch, calls)

    result = runner.invoke(main.app, ["task", "tidy up"])

    assert result.exit_code == 0, result.output
    assert calls == ["submit"]


def _status_with(monkeypatch, autonomy: str) -> str:
    def fake_api(method, path, **kwargs):
        if path == "/health":
            return _Reply({"status": "ok"})
        if path == "/orchestrator/settings":
            return _Reply({"power": "sport", "autonomy": autonomy})
        raise RuntimeError("not needed")

    monkeypatch.setattr(main, "_api", fake_api)
    result = runner.invoke(main.app, ["status"])
    assert result.exit_code == 0, result.output
    return result.output


def test_status_shows_the_dials_the_api_returns(monkeypatch) -> None:
    output = _status_with(monkeypatch, "safe")

    assert "sport" in output and "autonomy: safe" in output
    assert "YOLO" not in output


def test_status_shows_the_yolo_badge(monkeypatch) -> None:
    assert "YOLO" in _status_with(monkeypatch, "yolo")
