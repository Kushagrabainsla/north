"""Task submission from an external service."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from ledger.models import LedgerSource
from orchestrator.api.deps import _get_orchestrator
from orchestrator.models import TaskRequest
from utils.security import verify_secret

#
# External services (GitHub, calendar, email) POST here to trigger agent tasks.
# Authentication: pass the shared north secret in the X-Webhook-Secret header.
# Body (JSON): { "prompt": "...", "context": "..." }
# The source name becomes a prompt prefix so the classifier can route correctly.

webhook_router = APIRouter(
    prefix="/orchestrator",
    tags=["webhooks"],
    # No verify_request_secret dependency - we validate manually below to give
    # a clear 401 rather than the generic 403 from the cookie-based mechanism.
)


@webhook_router.post("/webhooks/{source}", status_code=202)
async def receive_webhook(source: str, request: Request) -> dict:
    """Receive an external event and submit it as a task.

    The ``source`` path parameter identifies the origin (e.g. ``gmail``,
    ``github``, ``calendar``).  The request body must be JSON with at least
    a ``prompt`` key.  Optionally include ``context`` for additional facts
    that should be injected as task context.

    Authentication is via the ``X-Webhook-Secret`` header - same secret as
    the rest of the API.
    """
    webhook_secret = request.headers.get("X-Webhook-Secret", "")
    if not webhook_secret or not verify_secret(webhook_secret):
        raise HTTPException(status_code=401, detail="Invalid or missing X-Webhook-Secret header.")

    try:
        body = await request.json()
    except Exception:
        body = {}

    prompt = str(body.get("prompt") or body.get("message") or f"Process incoming {source} event")
    context = str(body.get("context", ""))

    task_req = TaskRequest(
        prompt=f"[webhook:{source}] {prompt}",
        source=LedgerSource.WEBHOOK,
        context=context,
    )

    orch = _get_orchestrator()
    result = await orch.submit_task(task_req)
    return {"task_id": result.task_id, "status": result.status, "source": source}


