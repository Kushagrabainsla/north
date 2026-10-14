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


def test_ledger_times_show_in_the_time_zone_chosen_in_settings(monkeypatch) -> None:
    """The CLI reads the saved zone itself, so it agrees with the dashboard even offline."""
    import json

    import utils.time as north_time
    from config.settings import settings

    monkeypatch.setattr(north_time, "_configured_timezone_name", None)
    (settings.north_home / "settings.json").write_text(json.dumps({"timezone": "Asia/Kolkata"}), encoding="utf-8")
    entry = {"timestamp": "2026-10-14T00:00:00+00:00", "source": "prompt", "action": "received", "status": "pending"}
    monkeypatch.setattr(main, "_api", lambda method, path, **kw: _Reply([entry]))

    result = runner.invoke(main.app, ["ledger"])

    assert result.exit_code == 0, result.output
    assert "2026-10-14 05:30" in result.output
