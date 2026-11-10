"""Views north makes for itself: checked, kept as a draft you can open, added on your OK.

A view is A2UI v0.9 JSON on north's kit catalog. These tests hold the checks that stop a
view that could not be drawn, or that would act, from reaching the Views page.
"""

from __future__ import annotations

import asyncio
import copy
import json
import re

import pytest

from tools.models import ToolInput
from tools.universal.create_view import CreateViewTool
from utils.runtime_resources import builtin_skills_dir
from views import CATALOG_ID, ViewStore, check_view, messages
from views.catalog import COMPONENTS, catalog_document


class _Tool:
    def __init__(self, changes: bool) -> None:
        self._changes = changes

    def mutates(self, params=None) -> bool:
        return self._changes


class _Registry:
    def __init__(self, items: dict) -> None:
        self._items = items

    def get(self, name: str):
        if name not in self._items:
            raise KeyError(name)
        return self._items[name]


TOOLS = _Registry({"list_schedules": _Tool(False), "write_file": _Tool(True)})
FLOWS = _Registry({"nightly-cleanup": object()})

VIEW = {
    "id": "schedules-board",
    "title": "What runs when",
    "data": [{"name": "schedules", "tool": "list_schedules", "args": {}}],
    "actions": {"run_now": {"flow": "nightly-cleanup", "label": "Runs the cleanup now"}},
    "components": [
        {"id": "root", "component": "Column", "children": ["list", "run"]},
        {
            "id": "list",
            "component": "Panel",
            "title": "Schedules",
            "children": {"path": "/schedules/recurring", "componentId": "row"},
        },
        {"id": "row", "component": "ListItem", "title": {"path": "name"}, "meta": [{"path": "next_run"}]},
        {"id": "run", "component": "Button", "label": "Run the cleanup", "action": {"event": {"name": "run_now"}}},
    ],
}


def _check(view) -> list[str]:
    from views.document import reference_checks

    tool_problem, flow_problem = reference_checks(TOOLS, FLOWS)
    return check_view(view, tool_problem=tool_problem, flow_problem=flow_problem)


def _with(change) -> dict:
    view = copy.deepcopy(VIEW)
    change(view)
    return view


def test_a_view_built_from_the_kit_passes() -> None:
    assert _check(VIEW) == []


@pytest.mark.parametrize(
    ("change", "says"),
    [
        (lambda v: v["components"].pop(0), "'root'"),
        (lambda v: v["components"].append({"id": "x", "component": "Carousel"}), "no part named 'Carousel'"),
        (lambda v: v["components"][0]["children"].append("missing"), "'missing', which is not a component"),
        (lambda v: v["components"][1].update(children=["root"]), "contains itself"),
        (lambda v: v["components"].append({"id": "lost", "component": "Text", "text": "hi"}), "never shown: lost"),
        (lambda v: v["components"][2].update(title={"path": "/nothing/here"}), "no data named 'nothing'"),
        (
            lambda v: v["components"][3]["action"]["event"].update(name="explode"),
            "'explode', which is not one of the view's actions",
        ),
        (lambda v: v["data"].append({"name": "w", "tool": "write_file", "args": {}}), "write_file changes things"),
        (lambda v: v["data"].append({"name": "q", "tool": "no_such_tool"}), "no tool named 'no_such_tool'"),
        (lambda v: v["actions"].update(other={"flow": "gone"}), "action 'other'"),
        (lambda v: v["components"][2].update(onclick="alert(1)"), "Additional properties"),
        (lambda v: v.update(id="../../etc"), "does not match"),
    ],
)
def test_a_view_that_could_not_be_drawn_or_would_act_is_refused(change, says) -> None:
    problems = _check(_with(change))
    assert any(says in problem for problem in problems), problems


def test_a_link_goes_only_to_a_dashboard_page() -> None:
    def link(to: str):
        return _with(
            lambda v: (
                v["components"][0]["children"].append("go"),
                v["components"].append({"id": "go", "component": "Link", "label": "Go", "to": to}),
            )
        )

    assert _check(link("/flows")) == []
    assert _check(link("https://example.com"))
    assert _check(link("//example.com"))


def test_the_view_is_sent_as_a2ui_v0_9_messages_on_the_kit_catalog() -> None:
    sent = messages(VIEW, {"schedules": {"recurring": []}})
    assert [next(key for key in message if key != "version") for message in sent] == [
        "createSurface",
        "updateComponents",
        "updateDataModel",
    ]
    assert {message["version"] for message in sent} == {"v0.9"}
    assert sent[0]["createSurface"]["catalogId"] == CATALOG_ID
    assert sent[2]["updateDataModel"] == {
        "surfaceId": "schedules-board",
        "path": "/",
        "value": {"schedules": {"recurring": []}},
    }


def test_the_catalog_lists_each_part_once_with_its_schema() -> None:
    document = catalog_document()
    assert document["catalogId"] == CATALOG_ID
    assert set(document["components"]) == set(COMPONENTS)
    assert document["components"]["Button"]["properties"]["component"] == {"const": "Button"}


def test_a_draft_is_not_on_the_views_page_until_it_is_added(tmp_path) -> None:
    store = ViewStore(tmp_path)
    store.save_draft(VIEW)
    assert store.all() == []
    assert store.get("schedules-board", draft=True)["title"] == "What runs when"
    assert store.publish("schedules-board")["id"] == "schedules-board"
    assert [view["id"] for view in store.all()] == ["schedules-board"]
    assert store.get("schedules-board", draft=True) is None
    assert store.delete("schedules-board")
    assert store.all() == []


def test_a_view_id_cannot_name_a_file_outside_the_views_folder(tmp_path) -> None:
    store = ViewStore(tmp_path / "views")
    for bad in ("../secrets", "a/b", "", "UPPER"):
        with pytest.raises(ValueError):
            store.get(bad)


def test_create_view_drafts_then_adds_and_only_adding_or_removing_asks(tmp_path) -> None:
    store = ViewStore(tmp_path)
    tool = CreateViewTool(tool_registry=TOOLS, flow_registry=FLOWS, store=store)
    assert not tool.mutates({"action": "draft"})
    assert tool.mutates({"action": "add"}) and tool.mutates({"action": "remove"})

    refused = asyncio.run(
        tool.run(ToolInput(params={"action": "draft", "view": _with(lambda v: v["components"].pop(0))}))
    )
    assert not refused.success and "'root'" in refused.error
    assert store.get("schedules-board", draft=True) is None

    drafted = asyncio.run(tool.run(ToolInput(params={"action": "draft", "view": VIEW})))
    assert drafted.success and drafted.data["preview"] == "/app/#/views/schedules-board?draft=1"

    card = asyncio.run(tool.describe(ToolInput(params={"action": "add", "id": "schedules-board"})))
    assert card.title == "Add the view 'What runs when'?"
    assert "list_schedules (as schedules)" in card.message
    assert "runs the flow nightly-cleanup" in card.message or "Runs the cleanup now" in card.message
    assert "(/app/#/views/schedules-board?draft=1)" in card.message

    added = asyncio.run(tool.run(ToolInput(params={"action": "add", "id": "schedules-board"})))
    assert added.success and [view["id"] for view in store.all()] == ["schedules-board"]


def test_the_guides_example_passes_every_check() -> None:
    guide = (builtin_skills_dir() / "authoring-a-north-view" / "SKILL.md").read_text(encoding="utf-8")
    block = next(b for b in re.findall(r"```json\n(.*?)```", guide, re.S) if '"action": "draft"' in b)
    call = json.loads(block)
    assert _check(call["view"]) == []
