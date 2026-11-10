"""CreateViewTool - north makes a page for itself on the dashboard, from the UI kit only.

A view is A2UI v0.9 JSON on north's kit catalog (`views.catalog`): what it shows, the
read-only tools it reads when opened, and the flows its buttons run. `draft` checks it and
keeps it where only a link reaches it, so you can open it first; `add` puts the draft on
the Views page, and is the one call that asks. Its buttons never act by themselves: each
starts a flow, whose steps ask as usual.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from config.settings import settings
from tools.base import Tool
from tools.models import ToolInput, ToolOutput
from views import ViewStore, check_view, describe_catalog, summary
from views.document import reference_checks

GUIDE = "authoring-a-north-view"


def preview_link(view_id: str) -> str:
    return f"/app/#/views/{view_id}?draft=1"


class CreateViewTool(Tool):
    """Draft, check and add views north makes for itself; list, read and remove them."""

    name = "create_view"
    is_mutating = True
    description = (
        "Make a page for yourself on the dashboard's Views page, drawn only from north's UI kit: a status board, "
        "a list of a flow's runs, figures you track. Read the authoring-a-north-view skill first. action='parts' "
        "lists the parts a view may use. action='draft' checks a view (A2UI v0.9 JSON: id, title, data, actions, "
        "components) and keeps it as a draft with a preview link; fix every problem it names and draft again. "
        "action='add' puts the checked draft on the Views page, and asks first. A view reads only tools that "
        "change nothing, and its buttons only run flows, whose steps ask as usual. list, read and remove do what "
        "they say."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["parts", "draft", "add", "list", "read", "remove"]},
            "view": {"type": "object", "description": "draft only: the whole view document."},
            "id": {"type": "string", "description": "add, read, remove: the view's id."},
        },
        "required": ["action"],
    }

    def __init__(self, tool_registry=None, flow_registry=None, store: ViewStore | None = None) -> None:
        self._tool_registry = tool_registry
        self._flow_registry = flow_registry
        self._store = store or ViewStore(settings.north_home / "views")

    def mutates(self, params: dict[str, Any] | None = None) -> bool:
        # A draft is seen only through its link, so keeping one changes nothing you see.
        return str((params or {}).get("action") or "").strip().lower() in {"add", "remove"}

    def _problems(self, view: Any) -> list[str]:
        tool_problem, flow_problem = reference_checks(self._tool_registry, self._flow_registry)
        return check_view(view, tool_problem=tool_problem, flow_problem=flow_problem)

    async def describe(self, input: ToolInput):
        request = await super().describe(input)
        action = str(input.params.get("action") or "").strip().lower()
        view_id = str(input.params.get("id") or "").strip()
        if action == "add":
            try:
                draft = self._store.get(view_id, draft=True)
            except ValueError:
                draft = None
            if draft is not None:
                return replace(
                    request,
                    title=f"Add the view '{draft['title']}'?",
                    message=(
                        f"Approving puts this view on the Views page.\n\n{summary(draft)}\n\n"
                        f"[Open it first]({preview_link(view_id)})"
                    ),
                )
        if action == "remove":
            return replace(
                request, title=f"Remove the view '{view_id}'?", message="Approving deletes it from the Views page."
            )
        return request

    async def run(self, input: ToolInput) -> ToolOutput:
        action = str(input.params.get("action") or "").strip().lower()
        view_id = str(input.params.get("id") or "").strip()
        try:
            if action == "parts":
                return ToolOutput(success=True, data={"parts": describe_catalog(), "guide": GUIDE})
            if action == "list":
                return ToolOutput(success=True, data={"views": [_listing(view) for view in self._store.all()]})
            if action == "read":
                view = self._store.get(view_id) or self._store.get(view_id, draft=True)
                if view is None:
                    return ToolOutput(success=False, failure_kind="not_found", error=f"No view {view_id!r}.")
                return ToolOutput(success=True, data={"view": view})
            if action == "draft":
                return self._draft(input.params.get("view"))
            if action == "add":
                return self._add(view_id)
            if action == "remove":
                if not self._store.delete(view_id):
                    return ToolOutput(success=False, failure_kind="not_found", error=f"No view {view_id!r} to remove.")
                return ToolOutput(success=True, data={"removed": view_id})
        except ValueError as exc:
            return ToolOutput(success=False, error=str(exc))
        return ToolOutput(success=False, error="action must be parts, draft, add, list, read or remove.")

    def _draft(self, view: Any) -> ToolOutput:
        problems = self._problems(view)
        if problems:
            return ToolOutput(
                success=False,
                error="The view cannot be shown yet: " + "; ".join(problems[:12]),
                data={"problems": problems},
            )
        self._store.save_draft(view)
        return ToolOutput(success=True, data={"id": view["id"], "draft": True, "preview": preview_link(view["id"])})

    def _add(self, view_id: str) -> ToolOutput:
        draft = self._store.get(view_id, draft=True)
        if draft is None:
            return ToolOutput(success=False, error=f"No draft {view_id!r}: draft the view first.")
        # Checked again: a tool or flow it names may have gone since it was drafted.
        if problems := self._problems(draft):
            return ToolOutput(success=False, error="The draft cannot be shown: " + "; ".join(problems[:12]))
        live = self._store.publish(view_id)
        return ToolOutput(
            success=True, data={"id": view_id, "added": True, "page": f"/app/#/views/{view_id}", "title": live["title"]}
        )

    def format_output(self, data: dict[str, Any]) -> str:
        if "parts" in data:
            return f"Parts a view may use (see the {data['guide']} skill for the format):\n{data['parts']}"
        if data.get("draft"):
            return (
                f"Draft '{data['id']}' is checked. Preview: {data['preview']}. "
                f"Call add with id='{data['id']}' to put it on the Views page."
            )
        if data.get("added"):
            return f"The view '{data['title']}' is on the Views page: {data['page']}"
        if "views" in data:
            return "\n".join(f"{v['id']} - {v['title']}" for v in data["views"]) or "No views yet."
        return str(data)


def _listing(view: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": view["id"],
        "title": view["title"],
        "description": view.get("description", ""),
        "about": view.get("about"),
    }
