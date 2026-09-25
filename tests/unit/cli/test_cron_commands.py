"""`north cron`: schedules run flows, so that is what the commands take and show."""

from __future__ import annotations

from typer.testing import CliRunner

import cli.main as main

runner = CliRunner()


class _Reply:
    def __init__(self, payload) -> None:
        self._payload = payload

    def json(self):
        return self._payload


def _entry(**patch):
    return {
        "name": "user_briefing",
        "schedule": "daily at 07:00 (America/Los_Angeles)",
        "next_run_local": "2026-09-25 07:00 PDT",
        "task": "Compile the briefing",
        "flow": "daily-news-briefing",
        "source": "user",
        **patch,
    }


def test_adding_a_schedule_names_the_flow_and_sends_no_prompt_or_agent(monkeypatch) -> None:
    sent = {}

    def fake_api(method, path, **kwargs):
        sent.update(method=method, path=path, body=kwargs.get("json"))
        return _Reply(_entry())

    monkeypatch.setattr(main, "_api", fake_api)

    result = runner.invoke(main.app, ["cron", "add", "daily-news-briefing", "--hour", "7", "--label", "News"])

    assert result.exit_code == 0, result.output
    assert sent["method"] == "POST" and sent["path"] == "/orchestrator/cron"
    assert sent["body"]["flow"] == "daily-news-briefing"
    assert sent["body"]["label"] == "News"
    assert "task" not in sent["body"] and "agent" not in sent["body"]


def test_setting_a_schedule_can_retarget_its_flow_but_has_no_prompt_option(monkeypatch) -> None:
    sent = {}
    monkeypatch.setattr(main, "_api", lambda method, path, **kw: sent.update(body=kw.get("json")) or _Reply(_entry()))

    ok = runner.invoke(main.app, ["cron", "set", "user_briefing", "--flow", "other-flow"])
    refused = runner.invoke(main.app, ["cron", "set", "user_briefing", "--task", "do something else"])

    assert ok.exit_code == 0, ok.output
    assert sent["body"] == {"flow": "other-flow"}
    assert refused.exit_code != 0


def test_the_list_shows_the_flow_each_schedule_runs(monkeypatch) -> None:
    monkeypatch.setattr(main, "_api", lambda *a, **kw: _Reply([_entry(), _entry(name="old", flow="", task="stretch")]))
    # Wide enough that the table does not wrap a flow name across two lines.
    monkeypatch.setenv("COLUMNS", "200")

    result = runner.invoke(main.app, ["cron", "list"])

    assert result.exit_code == 0, result.output
    assert "daily-news-briefing" in result.output
    # One made before schedules ran flows still shows what it runs.
    assert "stretch" in result.output
