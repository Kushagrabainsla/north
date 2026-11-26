"""Custom panels: a view's inputs set values, and its parts call tools as you.

A view was a read-only page whose buttons could only start flows. A panel now holds its own
`state`, inputs bound both ways (text, select, switch, slider, colour, microphone), images,
a refresh time, and actions that call a tool. A call is yours, so it runs at once with no
approval card (CODING_STYLE §7.3); a call that sends, spends, deletes, pushes, runs a
command or changes north shows its exact values first and runs only once that exact call
is confirmed.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
from fastapi import HTTPException

from approval.approvals import Request
from approval.policy import Action, ActionKind, needs_your_confirmation
from orchestrator.api_context import ApiServices, bind_services
from tools.base import Tool
from tools.models import ToolInput, ToolOutput
from tools.registry import ToolRegistry
from views import ViewStore, check_view, messages, summary
from views.document import action_tool_check, reference_checks
from web import views as view_routes


class _Lights(Tool):
    """A device tool: `list` reads, `set` changes one light."""

    name = "lights"
    description = "lights"
    parameters_schema = {
        "type": "object",
        "properties": {"action": {"type": "string"}, "id": {"type": "string"}, "on": {"type": "boolean"}},
    }
    calls: list[dict[str, Any]] = []

    def mutates(self, params=None) -> bool:
        return (params or {}).get("action") != "list"

    async def describe(self, input: ToolInput) -> Request | None:
        action = Action(agent=self.name, kind=ActionKind.DEVICE, summary=f"set {input.params}")
        return Request(action=action, title="Set a light", message=f"Set {input.params}")

    async def run(self, input: ToolInput) -> ToolOutput:
        if input.params.get("action") == "list":
            return ToolOutput(success=True, data={"devices": [{"id": "lamp", "name": "Lamp", "on": False}]})
        type(self).calls.append(dict(input.params))
        return ToolOutput(success=True, data={"set": input.params.get("id")})


class _Mail(Tool):
    """A send: it reaches a third party, so a panel shows it before it runs."""

    name = "send_mail"
    description = "sends an email"
    is_mutating = True
    parameters_schema = {"type": "object", "properties": {"to": {"type": "string"}, "body": {"type": "string"}}}
    sent: list[dict[str, Any]] = []

    async def describe(self, input: ToolInput) -> Request | None:
        action = Action(agent=self.name, kind=ActionKind.OTHER, summary="send", reaches_third_party=True)
        return Request(action=action, title="Send this email?", message=f"To {input.params.get('to')}")

    async def run(self, input: ToolInput) -> ToolOutput:
        type(self).sent.append(dict(input.params))
        return ToolOutput(success=True, data={"sent": True})


class _NoCards:
    """An approval layer that fails the test if a panel's own call asks it anything."""

    def __getattr__(self, name: str):
        raise AssertionError(f"a panel's call asked the approval layer ({name})")


PANEL: dict[str, Any] = {
    "id": "lights-panel",
    "title": "Lights",
    "data": [{"name": "lights", "tool": "lights", "args": {"action": "list"}}],
    "state": {"level": 40, "note": "", "sound": {}},
    "refresh": 30,
    "actions": {
        "switch": {"tool": "lights", "args": {"action": "set", "id": {"path": "id"}, "on": {"path": "on"}}},
        "mail": {"tool": "send_mail", "args": {"to": "friend@example.com", "body": {"path": "/state/note"}}},
        "music": {"tool": "lights", "args": {"action": "set", "id": "lamp", "on": {"path": "/state/sound/beat"}}},
    },
    "components": [
        {"id": "root", "component": "Column", "children": ["list", "note", "send", "level", "hue", "mic", "pic"]},
        {
            "id": "list",
            "component": "Panel",
            "title": "Lights",
            "children": {"path": "/lights/devices", "componentId": "row"},
        },
        {
            "id": "row",
            "component": "Switch",
            "label": {"path": "name"},
            "value": {"path": "on"},
            "action": {"event": {"name": "switch"}},
        },
        {"id": "note", "component": "TextField", "label": "Note", "value": {"path": "/state/note"}},
        {"id": "send", "component": "Button", "label": "Send", "action": {"event": {"name": "mail"}}},
        {
            "id": "level",
            "component": "Slider",
            "label": "Level",
            "value": {"path": "/state/level"},
            "min": 0,
            "max": 100,
        },
        {"id": "hue", "component": "ColorPicker", "label": "Colour", "value": {"path": "/state/level"}},
        {
            "id": "mic",
            "component": "Microphone",
            "label": "Music",
            "value": {"path": "/state/sound"},
            "action": {"event": {"name": "music"}},
        },
        {"id": "pic", "component": "Image", "src": "https://example.com/a.png", "alt": "A picture"},
    ],
}


def _registry() -> ToolRegistry:
    registry = ToolRegistry(auto_register=False)
    for tool in (_Lights(), _Mail()):
        registry.register(tool)
        # Set after registering, which binds the registry's own approval layer.
        registry.get(tool.name).approvals = _NoCards()  # type: ignore[assignment]
    return registry


def _check(view: dict[str, Any], registry: ToolRegistry | None = None) -> list[str]:
    registry = registry or _registry()
    tool_problem, _ = reference_checks(registry, None)
    return check_view(view, tool_problem=tool_problem, action_tool_problem=action_tool_check(registry))


def _with(change) -> dict[str, Any]:
    view = copy.deepcopy(PANEL)
    change(view)
    return view


@pytest.fixture(autouse=True)
def _reset() -> None:
    _Lights.calls.clear()
    _Mail.sent.clear()


def test_a_panel_with_inputs_state_and_tool_actions_passes() -> None:
    assert _check(PANEL) == []


@pytest.mark.parametrize(
    ("change", "says"),
    [
        (lambda v: v["components"][3].update(value={"path": "/lights/devices"}), "an input writes to /state/<name>"),
        (lambda v: v["components"][3].update(value={"path": "/state/missing"}), "names no value in the view's state"),
        (lambda v: v["actions"]["mail"].update(tool="nope"), "there is no tool named 'nope'"),
        (lambda v: v["components"][2].update(action={"event": {"name": "gone"}}), "'gone', which is not one of"),
        (lambda v: v["components"][7].pop("action"), "'action' is a required property"),
        (lambda v: v.update(refresh=2), "less than the minimum"),
        (
            lambda v: v["data"].append({"name": "state", "tool": "lights", "args": {"action": "list"}}),
            "cannot be named",
        ),
        (lambda v: v["actions"]["mail"]["args"].update(body={"path": "/nothing/x"}), "no data named 'nothing'"),
    ],
)
def test_a_panel_that_could_not_work_is_refused(change, says) -> None:
    problems = _check(_with(change))
    assert any(says in problem for problem in problems), problems


def test_the_panel_starts_with_its_state_in_the_data() -> None:
    value = messages(PANEL, {"lights": {"devices": []}})[-1]["updateDataModel"]["value"]

    assert value["state"] == PANEL["state"] and value["lights"] == {"devices": []}


def test_the_card_that_adds_a_panel_says_what_each_action_does() -> None:
    text = summary(PANEL)

    assert "Action mail: calls send_mail" in text and '"to": "friend@example.com"' in text
    assert "asks you to confirm if it sends" in text
    assert "every 30 s" in text


def test_only_a_send_a_spend_a_delete_a_push_a_command_or_north_needs_your_confirmation() -> None:
    plain = Action(agent="lights", kind=ActionKind.DEVICE, summary="on")

    assert not needs_your_confirmation(plain)
    for fact in ("reaches_third_party", "spends_money", "obviously_destructive", "leaves_sandbox", "changes_north"):
        assert needs_your_confirmation(Action(agent="x", kind=ActionKind.OTHER, summary="s", **{fact: True}))
    for kind in (ActionKind.SHELL_COMMAND, ActionKind.TOOL_CHANGE, ActionKind.GITHUB):
        assert needs_your_confirmation(Action(agent="x", kind=kind, summary="s"))


async def _run(name: str, values: dict[str, Any], confirm: str = "", view: dict[str, Any] | None = None):
    ViewStore(view_routes.settings.north_home / "views").save_draft(view or PANEL)
    ViewStore(view_routes.settings.north_home / "views").publish((view or PANEL)["id"])
    with bind_services(ApiServices(north_home=view_routes.settings.north_home, tool_registry=_registry())):
        return await view_routes.run_action(
            (view or PANEL)["id"], name, view_routes.ActionCall(values=values, confirm=confirm)
        )


async def test_a_switch_in_a_row_calls_the_tool_at_once_with_that_rows_values_and_asks_nothing() -> None:
    result = await _run("switch", {"id": "lamp", "on": True})

    assert result["done"] is True
    assert _Lights.calls == [{"action": "set", "id": "lamp", "on": True}]


async def test_a_send_shows_its_exact_values_and_runs_only_once_that_call_is_confirmed() -> None:
    asked = await _run("mail", {"body": "hello"})

    assert asked["done"] is False and _Mail.sent == []
    assert asked["confirm"]["title"] == "Send this email?" and "friend@example.com" in asked["confirm"]["message"]

    other = await _run("mail", {"body": "something else"}, confirm=asked["confirm"]["fingerprint"])
    assert "confirm" in other and _Mail.sent == [], "a confirmation covers only the call that was shown"

    done = await _run("mail", {"body": "hello"}, confirm=asked["confirm"]["fingerprint"])
    assert done["done"] is True and _Mail.sent == [{"to": "friend@example.com", "body": "hello"}]


async def test_the_page_cannot_change_what_the_view_wrote() -> None:
    result = await _run("mail", {"body": "hi", "to": "attacker@example.com"})

    assert "friend@example.com" in result["confirm"]["message"]
    assert "attacker" not in result["confirm"]["message"]


async def test_a_bound_value_that_was_not_sent_is_refused() -> None:
    with pytest.raises(HTTPException) as missing:
        await _run("switch", {"id": "lamp"})

    assert "'on'" in missing.value.detail and _Lights.calls == []


async def test_confirm_true_in_the_view_asks_even_for_a_light() -> None:
    view = _with(lambda v: v["actions"]["switch"].update(confirm=True))

    asked = await _run("switch", {"id": "lamp", "on": True}, view=view)

    assert "confirm" in asked and _Lights.calls == []


async def test_the_open_view_tells_the_page_which_values_to_send_and_how_often_to_read() -> None:
    ViewStore(view_routes.settings.north_home / "views").save_draft(PANEL)
    ViewStore(view_routes.settings.north_home / "views").publish(PANEL["id"])
    with bind_services(ApiServices(north_home=view_routes.settings.north_home, tool_registry=_registry())):
        opened = await view_routes.open_view(PANEL["id"])

    assert opened["problems"] == []
    assert opened["bindings"]["switch"] == {"id": "id", "on": "on"}
    assert opened["bindings"]["mail"] == {"body": "/state/note"}, "written arguments stay on the server"
    assert opened["refresh"] == 30
