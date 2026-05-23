"""Web dashboard FastAPI router — Jinja2 + HTMX server-rendered UI.

Mounted at /ui on the main Orchestrator app (port 8000).

See docs/CODING_STYLE.md Sections 12, 17.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from approval.store import approval_store
from utils.security import load_secret
from context.models import ContextDocument
from jobs.models import JobStatus
from ledger.base import LedgerFilters, LedgerWriter
from ledger.models import LedgerStatus

router = APIRouter(prefix="/ui", tags=["web"])

_templates_dir = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_templates_dir))

# Module-level singletons populated by configure() at startup
_ledger: LedgerWriter | None = None
_agent_registry = None
_context_store = None
_context_injector = None
_job_processor = None
_inference_router = None
_confidence_tracker = None


def configure(
    ledger: LedgerWriter,
    agent_registry,
    context_store,
    context_injector,
    job_processor,
    inference_router,
    confidence_tracker,
) -> None:
    global _ledger, _agent_registry, _context_store, _context_injector
    global _job_processor, _inference_router, _confidence_tracker
    _ledger = ledger
    _agent_registry = agent_registry
    _context_store = context_store
    _context_injector = context_injector
    _job_processor = job_processor
    _inference_router = inference_router
    _confidence_tracker = confidence_tracker


def _get_ledger() -> LedgerWriter:
    if _ledger is None:
        raise RuntimeError("web routes not configured — call configure() at startup")
    return _ledger


# ── Auth ─────────────────────────────────────────────────────────────────────

@router.get("/auth", include_in_schema=False)
async def auth(request: Request, next: str = "/ui/") -> Response:
    """Set the shared secret as an HttpOnly session cookie then redirect.

    Visiting /ui/auth once per browser session authenticates the Web UI
    for all subsequent HTMX requests without embedding the secret in HTML.
    """
    secret = load_secret()
    response = RedirectResponse(url=next, status_code=302)
    response.set_cookie(
        key="north_secret",
        value=secret,
        httponly=True,
        samesite="strict",
        path="/",
    )
    return response


# ── Core pages ───────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def dashboard(request: Request) -> HTMLResponse:
    ledger = _get_ledger()
    entries = await ledger.query(LedgerFilters(limit=50))
    # Deduplicate active tasks: since entries are returned in DESC order of timestamp,
    # the first entry we encounter for a task_id is its latest status.
    task_latest_entries = {}
    for e in entries:
        if e.task_id is not None:
            if e.task_id not in task_latest_entries:
                task_latest_entries[e.task_id] = e

    active_tasks = [
        entry for entry in task_latest_entries.values()
        if entry.status == LedgerStatus.PENDING
    ]
    recent_entries = entries[:20]
    pending_approvals = approval_store.pending()

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "active_tasks": active_tasks,
            "recent_entries": recent_entries,
            "pending_approvals_count": len(pending_approvals),
        },
    )


@router.get("/ledger", response_class=HTMLResponse, include_in_schema=False)
async def ledger_view(request: Request, limit: int = 50) -> HTMLResponse:
    ledger = _get_ledger()
    entries = await ledger.query(LedgerFilters(limit=limit))

    return templates.TemplateResponse(
        request,
        "ledger.html",
        {
            "entries": entries,
            "limit": limit,
        },
    )


# ── Approvals ────────────────────────────────────────────────────────────────

@router.get("/approvals", response_class=HTMLResponse, include_in_schema=False)
async def approvals_view(request: Request) -> HTMLResponse:
    cards = approval_store.all(limit=50)
    pending = [c for c in cards if c.status == "pending"]
    resolved = [c for c in cards if c.status != "pending"]

    return templates.TemplateResponse(
        request,
        "approvals.html",
        {
            "pending": pending,
            "resolved": resolved,
        },
    )


@router.post("/approvals/respond", include_in_schema=False)
async def approvals_respond(request: Request) -> RedirectResponse:
    form = await request.form()
    card_id = str(form.get("card_id", ""))
    decision = str(form.get("decision", "approved"))

    approval_store.resolve(card_id, decision)
    return RedirectResponse(url="/ui/approvals", status_code=303)


# ── Context documents ─────────────────────────────────────────────────────────

@router.get("/context", response_class=HTMLResponse, include_in_schema=False)
async def context_index(request: Request) -> HTMLResponse:
    docs = [d.value for d in ContextDocument]
    return templates.TemplateResponse(
        request,
        "context_index.html",
        {"docs": docs},
    )


@router.get("/context/{doc_name}", response_class=HTMLResponse, include_in_schema=False)
async def context_doc(request: Request, doc_name: str) -> HTMLResponse:
    try:
        doc = ContextDocument(doc_name)
    except ValueError:
        return HTMLResponse(content="Unknown document", status_code=404)

    content = ""
    if _context_store is not None:
        content = await _context_store.read(doc)

    return templates.TemplateResponse(
        request,
        "context_doc.html",
        {"doc_name": doc_name, "content": content},
    )


@router.post("/context/{doc_name}", include_in_schema=False)
async def context_doc_save(request: Request, doc_name: str) -> RedirectResponse:
    try:
        doc = ContextDocument(doc_name)
    except ValueError:
        return RedirectResponse(url="/ui/context", status_code=303)

    form = await request.form()
    content = str(form.get("content", ""))
    if _context_store is not None:
        await _context_store.write(doc, content)

    return RedirectResponse(url=f"/ui/context/{doc_name}", status_code=303)


# ── Agents ────────────────────────────────────────────────────────────────────

@router.get("/agents", response_class=HTMLResponse, include_in_schema=False)
async def agents_view(request: Request) -> HTMLResponse:
    agents = _agent_registry.all() if _agent_registry else []
    return templates.TemplateResponse(
        request,
        "agents.html",
        {"agents": agents},
    )


# ── Jobs ──────────────────────────────────────────────────────────────────────

@router.get("/jobs", response_class=HTMLResponse, include_in_schema=False)
async def jobs_view(request: Request, status: str = "") -> HTMLResponse:
    jobs: list = []
    if _job_processor is not None:
        filter_status = JobStatus(status) if status else None
        jobs = await _job_processor.list_jobs(status=filter_status, limit=100)

    return templates.TemplateResponse(
        request,
        "jobs.html",
        {
            "jobs": jobs,
            "status_filter": status,
            "all_statuses": [s.value for s in JobStatus],
        },
    )


@router.post("/jobs/{job_id}/cancel", include_in_schema=False)
async def job_cancel(request: Request, job_id: str) -> RedirectResponse:
    if _job_processor is not None:
        await _job_processor.cancel(job_id)
    return RedirectResponse(url="/ui/jobs", status_code=303)


# ── Inference ─────────────────────────────────────────────────────────────────

@router.get("/inference", response_class=HTMLResponse, include_in_schema=False)
async def inference_view(request: Request) -> HTMLResponse:
    costs: dict = {}
    models: list = []
    if _inference_router is not None:
        try:
            costs = _inference_router.get_cost_summary()
        except Exception:
            costs = {}
        try:
            models = _inference_router.list_models()
        except Exception:
            models = []

    return templates.TemplateResponse(
        request,
        "inference.html",
        {"costs": costs, "models": models},
    )
