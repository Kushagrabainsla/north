"""`north ledger`: bare, it shows recent entries, as the README says, not the help."""

from __future__ import annotations

from typer.testing import CliRunner

import cli.main as main

runner = CliRunner()


class _Reply:
    def __init__(self, payload) -> None:
        self._payload = payload

    def json(self):
        return self._payload


def test_bare_ledger_shows_recent_entries(monkeypatch) -> None:
    asked = {}

    def fake_api(method, path, **kwargs):
        asked.update(path=path, params=kwargs.get("params"))
        return _Reply([])

    monkeypatch.setattr(main, "_api", fake_api)

    result = runner.invoke(main.app, ["ledger"])

    assert result.exit_code == 0, result.output
    assert asked == {"path": "/orchestrator/ledger", "params": {"limit": 20}}
    assert "no ledger entries" in result.output and "Usage" not in result.output


def test_ledger_search_still_runs_as_a_subcommand(monkeypatch) -> None:
    paths = []
    monkeypatch.setattr(main, "_api", lambda method, path, **kw: paths.append(path) or _Reply([]))

    result = runner.invoke(main.app, ["ledger", "search", "flight"])

    assert result.exit_code == 0, result.output
    assert paths == ["/orchestrator/ledger/search"]
