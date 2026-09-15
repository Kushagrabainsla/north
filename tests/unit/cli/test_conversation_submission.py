from __future__ import annotations

from cli import main as cli


class _Response:
    def __init__(self, payload: dict[str, str]) -> None:
        self._payload = payload

    def json(self) -> dict[str, str]:
        return self._payload


def test_one_shot_task_is_recorded_as_a_cli_conversation(monkeypatch) -> None:
    calls: list[tuple[str, str, dict]] = []

    def fake_api(method: str, path: str, **kwargs):
        calls.append((method, path, kwargs["json"]))
        if path == "/web/api/conversations":
            return _Response({"id": "conversation_1"})
        return _Response({"task_id": "task_1"})

    monkeypatch.setattr(cli, "_api", fake_api)

    assert cli._submit_task("inspect the repository", "/tmp/project") == "task_1"
    assert calls == [
        (
            "POST",
            "/web/api/conversations",
            {"title": "New chat", "source": "cli", "workspace": "/tmp/project"},
        ),
        (
            "POST",
            "/web/api/conversations/conversation_1/turns",
            {"prompt": "inspect the repository"},
        ),
    ]
