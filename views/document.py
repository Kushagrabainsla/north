"""A view north made: what it is called, what it reads, what its buttons do, and its parts.

    {
      "id": "nightly-cleanup-runs",
      "title": "Nightly cleanup",
      "description": "Each night's cleanup run and how it went.",
      "about": {"kind": "flow", "name": "nightly-cleanup"},
      "data": [{"name": "runs", "tool": "list_flow_runs", "args": {"flow": "nightly-cleanup"}}],
      "actions": {"run_now": {"flow": "nightly-cleanup", "label": "Runs the cleanup now"}},
      "components": [ ...A2UI v0.9 components from views.catalog, one with id "root"... ]
    }

`data` is read when the view opens, from tools that change nothing; each result is at
`/<name>` in the view's data. A button runs an `action`, which runs a flow: the flow's
own steps ask before acting, as any run does. Nothing in a view acts on its own.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from jsonschema import Draft202012Validator

from views.catalog import A2UI_VERSION, CATALOG_ID, COMPONENTS, CONTAINERS, ID_PATTERN, component_schema

SLUG = r"^[a-z0-9][a-z0-9-]{0,62}$"
MAX_COMPONENTS = 200
MAX_DEPTH = 12
ABOUT_KINDS = ("flow", "skill", "tool")

VIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "pattern": SLUG},
        "title": {"type": "string", "minLength": 1, "maxLength": 80},
        "description": {"type": "string", "maxLength": 300},
        "about": {
            "type": "object",
            "properties": {"kind": {"enum": list(ABOUT_KINDS)}, "name": {"type": "string", "minLength": 1}},
            "required": ["kind", "name"],
            "additionalProperties": False,
        },
        "data": {
            "type": "array",
            "maxItems": 8,
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "pattern": ID_PATTERN},
                    "tool": {"type": "string", "minLength": 1},
                    "args": {"type": "object"},
                },
                "required": ["name", "tool"],
                "additionalProperties": False,
            },
        },
        "actions": {
            "type": "object",
            "maxProperties": 8,
            "propertyNames": {"pattern": ID_PATTERN},
            "additionalProperties": {
                "type": "object",
                "properties": {
                    "flow": {"type": "string", "minLength": 1},
                    "inputs": {"type": "object"},
                    "label": {"type": "string", "maxLength": 120},
                },
                "required": ["flow"],
                "additionalProperties": False,
            },
        },
        "components": {"type": "array", "minItems": 1, "maxItems": MAX_COMPONENTS, "items": {"type": "object"}},
        # Set by the store when it saves the view.
        "updated_at": {"type": "string"},
    },
    "required": ["id", "title", "components"],
    "additionalProperties": False,
}

_VIEW = Draft202012Validator(VIEW_SCHEMA)
_COMPONENT = {name: Draft202012Validator(component_schema(name)) for name in COMPONENTS}

# Says what is wrong with a reference, or None when it is fine.
ToolCheck = Callable[[str, dict[str, Any]], str | None]
FlowCheck = Callable[[str], str | None]


def reference_checks(tool_registry: Any, flow_registry: Any) -> tuple[ToolCheck, FlowCheck]:
    """The checks for what a view names, against north's live tools and flows.

    A view reads only from a tool that exists and, called with those arguments, changes
    nothing; its buttons run flows that exist.
    """

    def tool_problem(name: str, args: dict[str, Any]) -> str | None:
        if tool_registry is None:
            return "tools are not loaded yet"
        try:
            tool = tool_registry.get(name)
        except Exception:
            return f"there is no tool named {name!r}"
        if tool.mutates(args):
            return f"{name} changes things, so a view cannot read from it; a button can run a flow instead"
        return None

    def flow_problem(name: str) -> str | None:
        if flow_registry is None:
            return "flows are not loaded yet"
        try:
            flow_registry.get(name)
        except Exception as exc:
            return str(exc)
        return None

    return tool_problem, flow_problem


def _where(error) -> str:
    return "/".join(str(part) for part in error.absolute_path) or "the view"


def check_view(
    document: Any, *, tool_problem: ToolCheck | None = None, flow_problem: FlowCheck | None = None
) -> list[str]:
    """Every reason *document* cannot be shown, in words north can act on. Empty when it can."""
    errors = [f"{_where(e)}: {e.message}" for e in _VIEW.iter_errors(document)]
    if errors:
        return errors
    components: list[dict[str, Any]] = document["components"]
    by_id: dict[str, dict[str, Any]] = {}
    for index, component in enumerate(components):
        name = component.get("component")
        if name not in _COMPONENT:
            errors.append(f"components/{index}: no part named {name!r}; the parts are {', '.join(COMPONENTS)}")
            continue
        errors += [
            f"components/{index} ({component.get('id')}): {e.message}" for e in _COMPONENT[name].iter_errors(component)
        ]
        if component.get("id") in by_id:
            errors.append(f"components/{index}: the id {component['id']!r} is used twice")
        by_id[component.get("id", "")] = component
    if errors:
        return errors
    if "root" not in by_id:
        return ["One component must have the id 'root': the view starts there."]

    data_names = [source["name"] for source in document.get("data", [])]
    if len(set(data_names)) != len(data_names):
        errors.append("Two data sources share a name.")
    actions = document.get("actions", {})

    # Children and templates name parts that exist; the tree has no loops and is not too deep.
    reached: set[str] = set()

    def walk(component_id: str, trail: tuple[str, ...]) -> None:
        if component_id in trail:
            errors.append(f"{component_id!r} contains itself: {' > '.join((*trail, component_id))}")
            return
        if len(trail) >= MAX_DEPTH:
            errors.append(f"{component_id!r} is nested more than {MAX_DEPTH} deep")
            return
        component = by_id.get(component_id)
        if component is None:
            errors.append(f"{trail[-1]!r} names {component_id!r}, which is not a component")
            return
        reached.add(component_id)
        children = component.get("children")
        if component["component"] in CONTAINERS and children is not None:
            ids = children if isinstance(children, list) else [children["componentId"]]
            for child in ids:
                walk(child, (*trail, component_id))

    walk("root", ())
    unused = [component_id for component_id in by_id if component_id not in reached]
    if unused:
        errors.append(f"Not reachable from 'root', so never shown: {', '.join(unused)}")

    for component in components:
        for path in _paths(component):
            if path.startswith("/") and path.strip("/").split("/")[0] not in data_names:
                errors.append(
                    f"{component['id']!r} reads {path!r}, but the view has no data named {path.split('/')[1]!r}"
                )
        if component["component"] == "Button":
            name = component["action"]["event"]["name"]
            if name not in actions:
                errors.append(f"Button {component['id']!r} runs {name!r}, which is not one of the view's actions")

    for source in document.get("data", []):
        if tool_problem is not None and (problem := tool_problem(source["tool"], source.get("args") or {})):
            errors.append(f"data {source['name']!r}: {problem}")
    for name, action in actions.items():
        if flow_problem is not None and (problem := flow_problem(action["flow"])):
            errors.append(f"action {name!r}: {problem}")
    return errors


def _paths(node: Any) -> list[str]:
    """Every data path a component reads, its template list included."""
    if isinstance(node, dict):
        if set(node) == {"path"} and isinstance(node["path"], str):
            return [node["path"]]
        if set(node) == {"path", "componentId"}:
            return [node["path"]]
        return [path for key, value in node.items() if key not in {"id", "component"} for path in _paths(value)]
    if isinstance(node, list):
        return [path for item in node for path in _paths(item)]
    return []


def messages(document: dict[str, Any], data: dict[str, Any]) -> list[dict[str, Any]]:
    """The view as the A2UI v0.9 messages a renderer reads: the surface, its parts, its data."""
    surface = document["id"]
    return [
        {"version": A2UI_VERSION, "createSurface": {"surfaceId": surface, "catalogId": CATALOG_ID}},
        {"version": A2UI_VERSION, "updateComponents": {"surfaceId": surface, "components": document["components"]}},
        {"version": A2UI_VERSION, "updateDataModel": {"surfaceId": surface, "path": "/", "value": data}},
    ]


def summary(document: dict[str, Any]) -> str:
    """What a view reads and what its buttons do, for the card that asks to add it."""
    lines = [f"**{document['title']}**" + (f": {document['description']}" if document.get("description") else "")]
    if about := document.get("about"):
        lines.append(f"About the {about['kind']} {about['name']}.")
    reads = [f"{source['tool']} (as {source['name']})" for source in document.get("data", [])]
    lines.append(
        "Reads, when you open it: " + (", ".join(reads) if reads else "nothing") + ". These tools change nothing."
    )
    for name, action in document.get("actions", {}).items():
        what = action.get("label") or f"runs the flow {action['flow']}"
        lines.append(f"Button action {name}: {what} (the flow {action['flow']}; its steps ask as usual).")
    return "\n".join(lines)
