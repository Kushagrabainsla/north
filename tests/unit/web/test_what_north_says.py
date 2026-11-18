"""What north says about a file, a model call, its dials or its own state is what is so.

Issue #76: read_file ended every slice, a one-line read of line 2500 included, with
"(truncated at 10000 lines)", and on the direct path that note was the whole reply
(A19). write_file said "Created" for writes that replaced a file's content (A20).
Every routing record of a call answered first time read "0 endpoints · 0
called" (P13). `north status` said "power cruise" while a pinned model decided
(P14).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from cli import main
from inference.decisions import SUCCESS, RoutingDecision
from inference.routing.router import _finish
from orchestrator.api_context import ApiServices, bind_services
from tools.models import ToolInput
from tools.universal.write_file import WriteFileTool


async def test_a_write_that_replaced_a_file_says_so(tmp_path: Path) -> None:
    plan = tmp_path / "plan.txt"
    plan.write_text("the old plan")
    tool = WriteFileTool()

    replaced = await tool.run(ToolInput(params={"path": str(plan), "content": "new"}))
    created = await tool.run(ToolInput(params={"path": str(tmp_path / "fresh.txt"), "content": "new"}))

    assert tool.format_output(replaced.data).startswith("Replaced the content of")
    assert tool.format_output(created.data).startswith("Created")


async def test_the_system_tile_says_what_you_would_go_and_look_up(tmp_path: Path) -> None:
    """It said only "online" and the approval mode: now models, waits, browser profiles and the version."""
    from config.browser_profiles import BrowserProfile
    from config.strategy import NorthSettings
    from utils.version import NORTH_VERSION
    from web.api import _system_facts

    settings = NorthSettings(tmp_path / "settings.json")
    settings.set_browser_profiles(
        [
            BrowserProfile(id="school", name="School", purpose="x", context="existing", data_directory=str(tmp_path)),
            BrowserProfile(id="home", name="Home", purpose="x", context="existing", data_directory=str(tmp_path)),
        ]
    )

    async def connected(profile) -> bool:
        return profile.id == "school"

    wait = {"provider": "mistral", "model": "flash", "available_at_epoch": 2.0e9, "reason": "rate limited"}
    router = SimpleNamespace(rate_limit_status=lambda: [wait], provider_state=lambda name: ("ready", ""))
    registry = SimpleNamespace(get=lambda name: SimpleNamespace(existing_browser=SimpleNamespace(connected=connected)))
    with bind_services(ApiServices(north_settings=settings, inference_router=router, tool_registry=registry)):
        facts = await _system_facts()

    assert facts["autonomy"] == settings.autonomy.value and facts["version"] == NORTH_VERSION
    assert facts["waiting"] == [{"model": "mistral:flash", "until": 2.0e9, "reason": "rate limited"}]
    assert facts["browser"] == [{"name": "School", "connected": True}, {"name": "Home", "connected": False}]


def test_a_call_answered_first_time_counts_the_endpoint_that_answered() -> None:
    decision = RoutingDecision(part="planner", requirements={})
    decision.chose("claude-haiku-5-5", "anthropic")

    recorded = _finish(decision, SimpleNamespace(considered=1, skipped=[]))

    assert (recorded.outcome, recorded.endpoints, recorded.attempted) == (SUCCESS, 1, 1)


def test_status_names_the_default_model_and_each_kind_with_its_own_row(monkeypatch) -> None:
    answers = {
        "/health": {"status": "ok", "checks": {}},
        "/orchestrator/settings": {
            "power": "cruise",
            "autonomy": "ask",
            "models": {
                "default": {"model": "anthropic:claude-haiku-5-5", "power": "cruise"},
                "kinds": [
                    {"kind": "tasks", "choice": None},
                    {"kind": "background", "choice": {"model": "", "power": "eco"}},
                ],
            },
        },
    }
    monkeypatch.setattr(main, "_api", lambda method, path, **kw: SimpleNamespace(json=lambda: answers.get(path, {})))

    output = CliRunner().invoke(main.app, ["status"]).output

    assert "default anthropic:claude-haiku-5-5 · background auto (eco)" in output
    assert "· tasks" not in output  # Same as default: nothing to add
    assert output.count("models ") == 1  # the catalog line has its own name
