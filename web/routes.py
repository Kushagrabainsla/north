"""Web dashboard FastAPI router — Jinja2 + HTMX server-rendered UI.

Mounted at /ui on the main Orchestrator app (port 8000).

See docs/CODING_STYLE.md Sections 12, 17.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from config.dependencies import build_production_dependencies
from ledger import LedgerFilters, LedgerSource, LedgerStatus
from utils.security import verify_request_secret

router = APIRouter(prefix="/ui", tags=["web"])

_templates_dir = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_templates_dir))


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def dashboard(request: Request) -> HTMLResponse:
    """Main dashboard page."""
    deps = build_production_dependencies()

    pending_entries = await deps.ledger.query(LedgerFilters(limit=50))
    active_tasks = [
        e for e in pending_entries if e.status == LedgerStatus.PENDING
    ]
    recent_entries = pending_entries[:20]

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "active_tasks": active_tasks,
            "recent_entries": recent_entries,
        },
    )


@router.get("/ledger", response_class=HTMLResponse, include_in_schema=False)
async def ledger_view(request: Request, limit: int = 50) -> HTMLResponse:
    """Full ledger view with recent entries."""
    deps = build_production_dependencies()
    entries = await deps.ledger.query(LedgerFilters(limit=limit))

    return templates.TemplateResponse(
        "ledger.html",
        {
            "request": request,
            "entries": entries,
            "limit": limit,
        },
    )
