"""The dashboard's Views page: the pages north made for itself, drawn from the UI kit.

A view's data is read when you open it, from tools that change nothing, through the same
`Tool.execute` every call goes through, so secrets stay hidden from it. A part you use starts
a flow the way the Flows page's Run does (the flow's steps ask before acting, as any run's
do), or makes a tool call as you, with no card: you are the one deciding (CODING_STYLE §7.3).
A call that sends, spends, deletes, pushes, runs a command or changes north comes back first
with its exact values, and runs once you confirm that very call. Deleting a view is yours
alone; adding or changing one is north's, through an approval card.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from approval.policy import needs_your_confirmation
from config.settings import settings
from orchestrator.api_context import current_services
from tools.models import ToolInput
from views import ViewStore, catalog_document, check_view, messages
from views.document import action_tool_check, reference_checks

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
    problems = check_view(
        view,
        tool_problem=tool_problem,
        flow_problem=flow_problem,
        action_tool_problem=action_tool_check(current_services().tool_registry),
    )
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
        # Which arguments the page reads when you use a part, and from where; the written ones stay here.
        "bindings": {name: _bound(action) for name, action in view.get("actions", {}).items()},
        "refresh": view.get("refresh", 0),
        "messages": messages(view, data) if not problems else [],
    }


class ActionCall(BaseModel):
    """What the browser sends when you use a part: the values its bound arguments read, and a confirmation."""

    model_config = ConfigDict(extra="forbid")

    # {argument name: value}, for the arguments the action binds with {"path": ...}. Written ones are the view's.
    values: dict[str, Any] = Field(default_factory=dict, max_length=12)
    # The fingerprint of the exact call you were shown and confirmed, for a call that must be confirmed.
    confirm: str = Field(default="", max_length=64)


def _arguments(written: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    """The action's arguments: each written one as the view has it, each bound one as the browser read it.

    The browser sends only the bound ones; a value for a name the view wrote is ignored, so a page can
    never change what the approved view says a button does.
    """
    out = {}
    for key, value in written.items():
        bound = isinstance(value, dict) and set(value) == {"path"}
        if bound and key not in values:
            raise HTTPException(status_code=400, detail=f"The value for {key!r} was not sent.")
        out[key] = values[key] if bound else value
    return out


def _bound(action: dict[str, Any]) -> dict[str, str]:
    """{argument: path} for each argument of *action* that is read when the part is used."""
    written = action.get("args") if "tool" in action else action.get("inputs")
    return {
        key: value["path"]
        for key, value in (written or {}).items()
        if isinstance(value, dict) and set(value) == {"path"}
    }


def _fingerprint(view_id: str, name: str, params: dict[str, Any]) -> str:
    text = json.dumps([view_id, name, params], sort_keys=True, default=str)
    return hashlib.sha256(text.encode()).hexdigest()[:32]


@router.post("/views/{view_id}/actions/{name}", status_code=202)
async def run_action(view_id: str, name: str, body: ActionCall | None = None) -> dict[str, Any]:
    """You used a part: start its flow, or make its tool call as you. A draft's parts do nothing until approved."""
    from web.extensions import FlowRunRequest, start_flow_run

    body = body or ActionCall()
    view = _view_or_404(view_id)
    action = view.get("actions", {}).get(name)
    if action is None:
        raise HTTPException(status_code=404, detail=f"The view has no action {name!r}.")
    if "flow" in action:
        inputs = _arguments(action.get("inputs") or {}, body.values)
        return await start_flow_run(action["flow"], FlowRunRequest(mode="execute", inputs=inputs))
    return await _call_as_you(view_id, name, action, _arguments(action.get("args") or {}, body.values), body.confirm)


async def _call_as_you(
    view_id: str, name: str, action: dict[str, Any], args: dict[str, Any], confirmed: str
) -> dict[str, Any]:
    """Make the action's tool call for you; first show its exact values when it must be confirmed."""
    registry = current_services().tool_registry
    if registry is None:
        raise HTTPException(status_code=503, detail="Tools are not loaded yet.")
    try:
        tool = registry.get(action["tool"])
    except Exception:
        raise HTTPException(status_code=404, detail=f"There is no tool named {action['tool']!r}.") from None
    prepared, error, request = await tool.prepare(ToolInput(params=args))
    if error:
        return {"done": False, "error": error}
    if request is not None and (action.get("confirm") or needs_your_confirmation(request.action)):
        fingerprint = _fingerprint(view_id, name, prepared.params)
        if confirmed != fingerprint:
            # Shown as the approval card would show it: the call's exact values, from the tool itself.
            return {
                "done": False,
                "confirm": {"title": request.title, "message": request.message, "fingerprint": fingerprint},
            }
    output = await tool.execute_for_you(prepared, request)
    logger.info("View %s: %s called %s as you (%s)", view_id, name, tool.name, "ok" if output.success else "failed")
    return {"done": output.success, "error": output.error or "", "data": output.data}


@router.delete("/views/{view_id}", status_code=204)
async def delete_view(view_id: str) -> None:
    try:
        removed = store().delete(view_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    if not removed:
        raise HTTPException(status_code=404, detail="No such view.")
