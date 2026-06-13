"""Web dashboard FastAPI router — Jinja2 + HTMX server-rendered UI.

Mounted at /ui on the main Orchestrator app (port 8000).

See docs/CODING_STYLE.md Sections 12, 17.
"""

from __future__ import annotations

from datetime import UTC
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from approval.store import ApprovalStore
from context.models import ContextDocument
from jobs.cron_store import UserCronStore
from jobs.models import JobStatus
from ledger.base import LedgerFilters, LedgerWriter
from ledger.models import LedgerStatus
from utils.security import SESSION_COOKIE, issue_session_token, verify_request_secret, verify_secret

# Every dashboard route requires the shared secret (header or session cookie),
# exactly like the API router. The dashboard can read the ledger, resolve
# approvals, and edit context documents — it must not be weaker than the API.
router = APIRouter(prefix="/ui", tags=["web"], dependencies=[Depends(verify_request_secret)])

# /ui/auth must stay reachable *without* the cookie (it is how the session is
# bootstrapped), so it lives on its own router and validates the secret itself.
auth_router = APIRouter(prefix="/ui", tags=["web"])

_LOGIN_FORM_HTML = """\
<h1>north — sign in</h1>
<p>Paste your north secret (from <code>~/.north/secret.key</code>). It is sent once
over a POST body and exchanged for a session cookie — never placed in a URL.</p>
<form method="post" action="/ui/auth">
  <input type="password" name="secret" autofocus autocomplete="off" />
  <input type="hidden" name="next" value="{next}" />
  <button type="submit">Sign in</button>
</form>
"""

_templates_dir = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_templates_dir))

# Module-level references populated by configure() at startup
_ledger: LedgerWriter | None = None
_agent_registry = None
_context_store = None
_context_injector = None
_job_processor = None
_inference_router = None
_confidence_tracker = None
_cron_store: UserCronStore | None = None
_approval_store: ApprovalStore | None = None


def configure(
    ledger: LedgerWriter,
    agent_registry,
    context_store,
    context_injector,
    job_processor,
    inference_router,
    confidence_tracker,
    cron_store: UserCronStore | None = None,
    approval_store: ApprovalStore | None = None,
) -> None:
    global _ledger, _agent_registry, _context_store, _context_injector
    global _job_processor, _inference_router, _confidence_tracker, _cron_store
    global _approval_store
    _ledger = ledger
    _agent_registry = agent_registry
    _context_store = context_store
    _context_injector = context_injector
    _job_processor = job_processor
    _inference_router = inference_router
    _confidence_tracker = confidence_tracker
    _cron_store = cron_store
    _approval_store = approval_store


def _get_ledger() -> LedgerWriter:
    if _ledger is None:
        raise RuntimeError("web routes not configured — call configure() at startup")
    return _ledger


# ── Auth ─────────────────────────────────────────────────────────────────────


def _safe_next(next: str) -> str:
    # Only same-site relative redirects — never to another origin.
    if not next.startswith("/") or next.startswith("//"):
        return "/ui/"
    return next


@auth_router.get("/auth", include_in_schema=False)
async def auth_form(request: Request, next: str = "/ui/") -> HTMLResponse:
    """Render the sign-in form.

    The secret is never accepted via the query string — URLs land in server
    logs, browser history, and Referer headers.
    """
    return HTMLResponse(content=_LOGIN_FORM_HTML.format(next=_safe_next(next)))


@auth_router.post("/auth", include_in_schema=False)
async def auth_submit(request: Request) -> Response:
    """Exchange the shared secret (POST body) for a signed session cookie.

    The cookie holds a signed, expiring session token — never the master
    secret — so a leaked cookie cannot be replayed as an API credential
    after expiry and never exposes the key itself.
    """
    form = await request.form()
    secret = str(form.get("secret", ""))
    next_url = _safe_next(str(form.get("next", "/ui/")))
    if not verify_secret(secret):
        return HTMLResponse(
            content="<h1>403 — Forbidden</h1><p>Invalid secret. <a href='/ui/auth'>Try again</a>.</p>",
            status_code=403,
        )
    response = RedirectResponse(url=next_url, status_code=303)
    response.set_cookie(
        key=SESSION_COOKIE,
        value=issue_session_token(),
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
        if e.task_id is not None and e.task_id not in task_latest_entries:
            task_latest_entries[e.task_id] = e

    active_tasks = [entry for entry in task_latest_entries.values() if entry.status == LedgerStatus.PENDING]
    recent_entries = entries[:20]
    pending_approvals = _approval_store.pending() if _approval_store else []

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
    cards = _approval_store.all(limit=50) if _approval_store else []
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

    if _approval_store is not None:
        _approval_store.resolve(card_id, decision)
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

    cron_entries: list = []
    if _cron_store is not None:
        cron_entries = await _cron_store.list()

    agent_names: list = []
    if _agent_registry is not None:
        agent_names = _agent_registry.names()

    return templates.TemplateResponse(
        request,
        "jobs.html",
        {
            "jobs": jobs,
            "status_filter": status,
            "all_statuses": [s.value for s in JobStatus],
            "cron_entries": cron_entries,
            "agent_names": agent_names,
        },
    )


@router.post("/jobs/{job_id}/cancel", include_in_schema=False)
async def job_cancel(request: Request, job_id: str) -> RedirectResponse:
    if _job_processor is not None:
        await _job_processor.cancel(job_id)
    return RedirectResponse(url="/ui/jobs", status_code=303)


@router.post("/jobs/schedule/oneshot", include_in_schema=False)
async def schedule_oneshot(request: Request) -> RedirectResponse:
    """Create a one-shot job from the web form."""
    from datetime import datetime

    from jobs.models import Job, JobPriority, JobType
    from utils.ids import generate_id

    form = await request.form()
    task = str(form.get("task", "")).strip()
    agent = str(form.get("agent", "general")).strip()
    run_at_str = str(form.get("run_at", "")).strip()

    if task and run_at_str and _job_processor is not None:
        try:
            scheduled_at = datetime.fromisoformat(run_at_str)
            if scheduled_at.tzinfo is None:
                scheduled_at = scheduled_at.replace(tzinfo=UTC)
            job = Job(
                job_id=generate_id(),
                type=JobType.ASYNC,
                agent=agent,
                task=task,
                payload={"scheduled_by": "web_ui"},
                priority=JobPriority.MEDIUM,
                scheduled_at=scheduled_at,
            )
            await _job_processor.enqueue(job)
        except (ValueError, Exception):
            pass

    return RedirectResponse(url="/ui/jobs", status_code=303)


@router.post("/jobs/schedule/recurring", include_in_schema=False)
async def schedule_recurring(request: Request) -> RedirectResponse:
    """Create a recurring cron entry from the web form."""
    import re

    form = await request.form()
    task = str(form.get("task", "")).strip()
    agent = str(form.get("agent", "general")).strip()
    name = str(form.get("name", "")).strip()
    hour_str = str(form.get("hour", "")).strip()
    minute_str = str(form.get("minute", "0")).strip()
    weekday_str = str(form.get("weekday", "")).strip()

    if task and hour_str and _cron_store is not None:
        try:
            hour = int(hour_str)
            minute = int(minute_str) if minute_str else 0
            weekday = int(weekday_str) if weekday_str else None
            entry_name = name or "user_" + re.sub(r"[^a-z0-9]+", "_", task.lower())[:40].strip("_")
            await _cron_store.add(
                name=entry_name,
                agent=agent,
                task=task,
                hour=hour,
                minute=minute,
                weekday=weekday,
            )
        except (ValueError, Exception):
            pass

    return RedirectResponse(url="/ui/jobs", status_code=303)


@router.post("/jobs/cron/{name}/delete", include_in_schema=False)
async def cron_delete(request: Request, name: str) -> RedirectResponse:
    if _cron_store is not None:
        await _cron_store.remove(name)
    return RedirectResponse(url="/ui/jobs", status_code=303)


# ── Inference ─────────────────────────────────────────────────────────────────


@router.get("/metrics", response_class=HTMLResponse, include_in_schema=False)
async def metrics_view(request: Request, days: int = 7) -> HTMLResponse:
    metrics: dict = {}
    if _ledger is not None:
        try:
            days = max(1, min(days, 365))
            metrics = await _ledger.get_metrics(days=days)
        except Exception:
            metrics = {}

    return templates.TemplateResponse(
        request,
        "metrics.html",
        {"metrics": metrics, "days": days},
    )


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
