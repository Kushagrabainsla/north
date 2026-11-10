"""The parts a north-made view may use: the dashboard's UI kit, as an A2UI v0.9 catalog.

A view is A2UI v0.9 JSON (https://a2ui.org/specification/v0.9-a2ui/): a flat list of
components, each with an `id`, a `component` name from this catalog and its properties,
one of them `root`. Containers name their children by id, or repeat one template over a
list in the data (`{"path": "/runs", "componentId": "run_row"}`). A property that shows
data binds to it with `{"path": "/runs/0/status"}`; inside a template a path without the
leading slash is read from the item. Each part here is drawn by the kit part of the same
role (web/src/views.tsx), so a view looks like the rest of the dashboard and can use only
what the kit has. A new kit part a view should use is added here, with its renderer.
"""

from __future__ import annotations

from typing import Any

CATALOG_ID = "north:kit/v1"
A2UI_VERSION = "v0.9"

ID_PATTERN = r"^[A-Za-z][A-Za-z0-9_-]{0,63}$"

# A value shown as is, or read from the data.
DYNAMIC: dict[str, Any] = {
    "anyOf": [
        {"type": "string", "maxLength": 2000},
        {"type": "number"},
        {"type": "boolean"},
        {
            "type": "object",
            "properties": {"path": {"type": "string", "minLength": 1, "maxLength": 200}},
            "required": ["path"],
            "additionalProperties": False,
        },
    ]
}
BINDING: dict[str, Any] = DYNAMIC["anyOf"][3]
CHILDREN: dict[str, Any] = {
    "anyOf": [
        {"type": "array", "items": {"type": "string", "pattern": ID_PATTERN}, "maxItems": 50},
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "minLength": 1, "maxLength": 200},
                "componentId": {"type": "string", "pattern": ID_PATTERN},
            },
            "required": ["path", "componentId"],
            "additionalProperties": False,
        },
    ]
}
TEXT = {"type": "string", "minLength": 1, "maxLength": 120}

# name -> (what it is for, its properties, which are required)
COMPONENTS: dict[str, tuple[str, dict[str, Any], list[str]]] = {
    "Column": ("Blocks one above the other, with the kit's gap.", {"children": CHILDREN}, ["children"]),
    "Row": ("Buttons or short parts side by side, wrapping on a phone.", {"children": CHILDREN}, ["children"]),
    "Panel": (
        "A titled block, like every panel on the dashboard.",
        {"title": TEXT, "label": TEXT, "children": CHILDREN},
        ["title", "children"],
    ),
    "Heading": (
        "A section title inside a panel, with an optional line under it.",
        {"text": DYNAMIC, "description": DYNAMIC},
        ["text"],
    ),
    "Text": ("A line or paragraph of plain text.", {"text": DYNAMIC}, ["text"]),
    "Note": ("A quieter line saying what a block is for.", {"text": DYNAMIC}, ["text"]),
    "Markdown": ("Formatted text: lists, links, code.", {"text": DYNAMIC}, ["text"]),
    "StatGrid": ("A few headline numbers side by side; its children are Stat.", {"children": CHILDREN}, ["children"]),
    "Stat": (
        "One headline number: what it counts, the value, a line under it.",
        {"label": TEXT, "value": DYNAMIC, "detail": DYNAMIC},
        ["label", "value"],
    ),
    "Table": (
        "A list of things of one kind with the same facts each: one row per item of `rows`.",
        {
            "rows": BINDING,
            "columns": {
                "type": "array",
                "minItems": 1,
                "maxItems": 6,
                "items": {
                    "type": "object",
                    "properties": {"key": {"type": "string", "minLength": 1, "maxLength": 64}, "label": TEXT},
                    "required": ["key", "label"],
                    "additionalProperties": False,
                },
            },
            "empty": TEXT,
        },
        ["rows", "columns"],
    ),
    "ListItem": (
        "One row of a short list: a title, a line about it, small facts, its state.",
        {
            "title": DYNAMIC,
            "description": DYNAMIC,
            "meta": {"type": "array", "items": DYNAMIC, "maxItems": 6},
            "status": DYNAMIC,
        },
        ["title"],
    ),
    "Status": (
        "A state as a word in the colour that word has everywhere (completed, failed...).",
        {"value": DYNAMIC},
        ["value"],
    ),
    "Badge": ("A tag that is not a state: where something came from, or its kind.", {"text": DYNAMIC}, ["text"]),
    "Notice": (
        "A message about the view: info, success, warning or error.",
        {"tone": {"enum": ["info", "success", "warning", "error"]}, "text": DYNAMIC},
        ["tone", "text"],
    ),
    "Empty": ("What shows when there is nothing to list.", {"text": DYNAMIC}, ["text"]),
    "KeyValues": (
        "Facts about one thing as label and value.",
        {
            "items": {
                "type": "array",
                "minItems": 1,
                "maxItems": 20,
                "items": {
                    "type": "object",
                    "properties": {"label": TEXT, "value": DYNAMIC},
                    "required": ["label", "value"],
                    "additionalProperties": False,
                },
            }
        },
        ["items"],
    ),
    "Button": (
        "Runs one of the view's actions when pressed. `action.event.name` names it.",
        {
            "label": TEXT,
            "variant": {"enum": ["primary", "secondary", "danger"]},
            "action": {
                "type": "object",
                "properties": {
                    "event": {
                        "type": "object",
                        "properties": {"name": {"type": "string", "pattern": ID_PATTERN}},
                        "required": ["name"],
                        "additionalProperties": False,
                    }
                },
                "required": ["event"],
                "additionalProperties": False,
            },
        },
        ["label", "action"],
    ),
    "Link": (
        "Goes to a dashboard page, such as /flows or /tasks/<id>.",
        {"label": TEXT, "to": {"type": "string", "pattern": r"^/(?!/)[^\s]{0,200}$"}},
        ["label", "to"],
    ),
}

# Containers, and where their children are named.
CONTAINERS = {"Column", "Row", "Panel", "StatGrid"}


def component_schema(name: str) -> dict[str, Any]:
    """The JSON Schema of one component of *name*."""
    _, properties, required = COMPONENTS[name]
    return {
        "type": "object",
        "properties": {
            "id": {"type": "string", "pattern": ID_PATTERN},
            "component": {"const": name},
            **properties,
        },
        "required": ["id", "component", *required],
        "additionalProperties": False,
    }


def catalog_document() -> dict[str, Any]:
    """The catalog as A2UI publishes one: an id and a JSON Schema per component."""
    return {
        "catalogId": CATALOG_ID,
        "$id": CATALOG_ID,
        "components": {name: {"description": COMPONENTS[name][0], **component_schema(name)} for name in COMPONENTS},
    }


def describe_catalog() -> str:
    """The catalog in a few lines, for north: each part, what it is for, and its properties."""
    lines = []
    for name, (purpose, properties, required) in COMPONENTS.items():
        props = ", ".join(f"{prop}{'' if prop in required else '?'}" for prop in properties)
        lines.append(f"- {name}({props}): {purpose}")
    return "\n".join(lines)
