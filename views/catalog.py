"""The parts a north-made view may use: the dashboard's UI kit, as an A2UI v0.9 catalog.

A view is A2UI v0.9 JSON (https://a2ui.org/specification/v0.9-a2ui/): a flat list of
components, each with an `id`, a `component` name from this catalog and its properties,
one of them `root`. Containers name their children by id, or repeat one template over a
list in the data (`{"path": "/runs", "componentId": "run_row"}`). A property that shows
data binds to it with `{"path": "/runs/0/status"}`; inside a template a path without the
leading slash is read from the item. An input's `value` is bound the same way, both
ways, as A2UI's inputs are: to `/state/<name>` (the view's own `state`) or to a field of
its row. Each part here is drawn by the kit part of the same role (web/src/views.tsx), so
a view looks like the rest of the dashboard and can use only what the kit has. A new kit
part a view should use is added here, with its renderer.
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
# One of the view's actions, run when a part is used: a button pressed, a switch turned, a slider let go.
EVENT: dict[str, Any] = {
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
}
# Where an input keeps what you set: a path in the view's `state`, or a field of the row it is in.
# The part shows that value and writes your change back to it; an action's args read it from there.
VALUE: dict[str, Any] = BINDING

# Parts you set a value with. Each may name an `action`, run with the new value in place.
INPUTS = {"TextField", "Select", "Switch", "Slider", "ColorPicker", "Microphone"}

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
        {"label": TEXT, "variant": {"enum": ["primary", "secondary", "danger"]}, "action": EVENT},
        ["label", "action"],
    ),
    "Link": (
        "Goes to a dashboard page, such as /flows or /tasks/<id>.",
        {"label": TEXT, "to": {"type": "string", "pattern": r"^/(?!/)[^\s]{0,200}$"}},
        ["label", "to"],
    ),
    "Image": (
        "A picture from the data: an https address or a data:image URI.",
        {"src": DYNAMIC, "alt": TEXT},
        ["src", "alt"],
    ),
    "TextField": (
        "A line you type in. `value` is where it keeps the text; `action` runs when you press Enter.",
        {"label": TEXT, "value": VALUE, "placeholder": TEXT, "action": EVENT},
        ["label", "value"],
    ),
    "Select": (
        "One choice from a short list. `action` runs when you pick one.",
        {
            "label": TEXT,
            "value": VALUE,
            "options": {
                "anyOf": [
                    {"type": "array", "minItems": 1, "maxItems": 50, "items": {"type": "string", "maxLength": 120}},
                    BINDING,
                ]
            },
            "action": EVENT,
        },
        ["label", "value", "options"],
    ),
    "Switch": (
        "On or off, such as a light. `action` runs when you turn it.",
        {"label": DYNAMIC, "value": VALUE, "action": EVENT},
        ["label", "value"],
    ),
    "Slider": (
        "A number in a range, such as a brightness. `action` runs when you let go.",
        {
            "label": DYNAMIC,
            "value": VALUE,
            "min": {"type": "number"},
            "max": {"type": "number"},
            "step": {"type": "number", "exclusiveMinimum": 0},
            "unit": {"type": "string", "maxLength": 8},
            "action": EVENT,
        },
        ["label", "value", "min", "max"],
    ),
    "ColorPicker": (
        "A colour as a hue, 0 to 360, on a rainbow slider. `action` runs when you let go.",
        {"label": DYNAMIC, "value": VALUE, "action": EVENT},
        ["label", "value"],
    ),
    "Microphone": (
        "Listens to music in the browser while you let it, and runs `action` as it plays, several times a second "
        "and on each beat. `value` is where it keeps what it hears: level (0-1), beat (true on a beat), and a "
        "colour that follows the music (hue, saturation, brightness). The sound never leaves the browser.",
        {
            "label": TEXT,
            "value": VALUE,
            "mode": {"enum": ["beat", "mood"]},
            "per_second": {"type": "integer", "minimum": 1, "maximum": 10},
            "action": EVENT,
        },
        ["label", "value", "action"],
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
