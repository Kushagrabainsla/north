"""The dashboard's Views page: the pages north made for itself, drawn from the UI kit.

A view's data is read when you open it, from tools that change nothing, through the same
`Tool.execute` every call goes through, so secrets stay hidden from it. Its buttons start a
flow the way the Flows page's Run does: the flow's steps ask before acting, as any run's do.
Deleting a view is yours alone; adding or changing one is north's, through an approval card.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, HTTPException

from config.settings import settings
from orchestrator.api_context import current_services
from tools.models import ToolInput
from views import ViewStore, catalog_document, check_view, messages
from views.document import reference_checks

logger = logging.getLogger(__name__)
router = APIRouter(tags=["web"])

# A view that waits longer than this for one source shows that source as failed.
READ_TIMEOUT_SECONDS = 20


def store() -> ViewStore:
    return ViewStore(settings.north_home / "views")


def checks():
    """What a view names, checked against this north's live tools and flows."""
    services = current_services()
    return reference_checks(services.tool_registry, services.flow_registry)


def _view_or_404(view_id: str, *, draft: bool = False) -> dict[str, Any]:
    try:
        view = store().get(view_id, draft=draft)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    if view is None:
        raise HTTPException(status_code=404, detail="No such view." if not draft else "No draft of this view.")
    return view


async def _read(source: dict[str, Any]) -> Any:
    """One data source's result, or {"error": why} when it could not be read."""
    args = source.get("args") or {}
    tool_problem, _ = checks()
    if problem := tool_problem(source["tool"], args):
        return {"error": problem}
    tool = current_services().tool_registry.get(source["tool"])
    try:
        output = await asyncio.wait_for(tool.execute(ToolInput(params=dict(args))), READ_TIMEOUT_SECONDS)
    except TimeoutError:
        return {"error": f"{source['tool']} did not answer within {READ_TIMEOUT_SECONDS}s"}
    except Exception as exc:
        logger.warning("View data %s failed: %s", source["tool"], exc)
        return {"error": f"{type(exc).__name__}: {exc}"}
    return output.data if output.success else {"error": output.error or "failed"}


def _listing(view: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": view["id"],
        "title": view["title"],
        "description": view.get("description", ""),
        "about": view.get("about"),
        "updated_at": view.get("updated_at", ""),
        "reads": [source["tool"] for source in view.get("data", [])],
        "actions": len(view.get("actions", {})),
    }


@router.get("/views")
async def list_views() -> list[dict[str, Any]]:
    return [_listing(view) for view in store().all()]


@router.get("/views-catalog")
async def view_catalog() -> dict[str, Any]:
    """The parts a view may use, as the A2UI catalog north writes against."""
    return catalog_document()


@router.get("/views/{view_id}")
async def open_view(view_id: str, draft: bool = False) -> dict[str, Any]:
    """The view as A2UI messages with its data read now, plus anything that stops it showing."""
    view = _view_or_404(view_id, draft=draft)
    tool_problem, flow_problem = checks()
    problems = check_view(view, tool_problem=tool_problem, flow_problem=flow_problem)
    sources = view.get("data", []) if not problems else []
    values = await asyncio.gather(*(_read(source) for source in sources))
    data = {source["name"]: value for source, value in zip(sources, values, strict=True)}
    return {
        **_listing(view),
        "draft": draft,
        "problems": problems,
        "failed": {
            name: value["error"] for name, value in data.items() if isinstance(value, dict) and set(value) == {"error"}
        },
        "action_labels": {name: action.get("label", "") for name, action in view.get("actions", {}).items()},
        "messages": messages(view, data) if not problems else [],
    }


@router.post("/views/{view_id}/actions/{name}", status_code=202)
async def run_action(view_id: str, name: str) -> dict[str, Any]:
    """A button pressed: start its flow. A draft's buttons do nothing until you approve it."""
    from web.extensions import FlowRunRequest, start_flow_run

    view = _view_or_404(view_id)
    action = view.get("actions", {}).get(name)
    if action is None:
        raise HTTPException(status_code=404, detail=f"The view has no action {name!r}.")
    return await start_flow_run(action["flow"], FlowRunRequest(mode="execute", inputs=action.get("inputs") or {}))


@router.delete("/views/{view_id}", status_code=204)
async def delete_view(view_id: str) -> None:
    try:
        removed = store().delete(view_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    if not removed:
        raise HTTPException(status_code=404, detail="No such view.")
