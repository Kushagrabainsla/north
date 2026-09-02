"""Request-scoped access to the components the HTTP layer serves.

`app.state.north_services` owns the wiring - one `ApiServices` per FastAPI app, so
two apps can coexist and a test can build its own without touching module state
(CODING_STYLE §22: no global mutable state, §6.3: injected at the boundary).

A router-level dependency binds that object to a context variable for the life of
each request, so route bodies and the helpers they call read it without threading
a parameter through every signature. This is the same mechanism
`utils/execution_context.py` uses to carry run identity through an agent call.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, fields
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Request

if TYPE_CHECKING:
    from agents.registry import AgentRegistry
    from approval.store import ApprovalStore
    from config.strategy import NorthSettings
    from inference.base import InferenceRouter
    from jobs.base import JobProcessor
    from jobs.cron_store import UserCronStore
    from ledger.base import LedgerWriter
    from memory.base import ContextStore
    from memory.injection import ContextInjector
    from orchestrator.agent_runs import AgentRunStore
    from orchestrator.orchestrator import Orchestrator
    from orchestrator.stream import EventStreamManager
    from tools.confidence import ConfidenceTracker

_STATE_ATTR = "north_services"


@dataclass(frozen=True)
class ApiServices:
    """Everything the HTTP layer is allowed to reach, wired once at startup.

    Frozen: routes read it, nothing rebinds it mid-flight. Fields are optional so
    a test can construct exactly the slice it exercises; `require()` raises a clear
    error rather than an AttributeError when a route needs one that was not wired.
    """

    orchestrator: Orchestrator | None = None
    stream_manager: EventStreamManager | None = None
    ledger: LedgerWriter | None = None
    agent_registry: AgentRegistry | None = None
    context_store: ContextStore | None = None
    context_injector: ContextInjector | None = None
    job_processor: JobProcessor | None = None
    inference_router: InferenceRouter | None = None
    confidence_tracker: ConfidenceTracker | None = None
    cron_store: UserCronStore | None = None
    north_settings: NorthSettings | None = None
    agent_run_store: AgentRunStore | None = None
    approval_store: ApprovalStore | None = None
    conversation_store: Any | None = None
    fact_store: Any | None = None
    skill_registry: Any | None = None
    north_home: Any | None = None
    # Mutable per-app runtime state for the web layer (in-flight logins,
    # the bootstrap task). Held here so it is per-app like the wiring.
    web_runtime: Any | None = None

    def require(self, name: str) -> Any:
        """Return a wired component, or raise naming the one that is missing."""
        value = getattr(self, name, None)
        if value is None:
            raise RuntimeError(f"{name} is not configured on this app")
        return value

    def replace(self, **overrides: Any) -> ApiServices:
        """A copy with *overrides* applied - the only way to 'change' a frozen set."""
        known = {f.name for f in fields(self)}
        unknown = set(overrides) - known
        if unknown:
            raise TypeError(f"unknown ApiServices field(s): {sorted(unknown)}")
        return ApiServices(**{**{f.name: getattr(self, f.name) for f in fields(self)}, **overrides})


_CURRENT: ContextVar[ApiServices | None] = ContextVar("north_api_services", default=None)


def attach(app: FastAPI, services: ApiServices) -> None:
    """Make *services* the wiring for *app*, replacing anything already there."""
    setattr(app.state, _STATE_ATTR, services)


def merge(app: FastAPI, **overrides: Any) -> ApiServices:
    """Add or replace individual components on *app*'s wiring.

    The orchestrator and web routers are mounted on one app and configured
    separately, so each contributes its own slice rather than replacing the
    whole set - which is what a plain `attach` from both would do, leaving
    whichever ran last in charge.
    """
    services = services_of(app).replace(**overrides)
    attach(app, services)
    return services


def services_of(app: FastAPI) -> ApiServices:
    """The services attached to *app*, or an empty set if none were attached."""
    return getattr(app.state, _STATE_ATTR, None) or ApiServices()


async def bind_request_services(request: Request) -> None:
    """Router-level dependency: bind this app's services for one request.

    FastAPI resolves router dependencies before the route body, and each request
    runs in its own context, so concurrent requests to different apps never see
    each other's wiring.
    """
    _CURRENT.set(services_of(request.app))


def current_services() -> ApiServices:
    """The services bound to the request being handled.

    Returns an empty set outside a request, so callers get a precise
    "X is not configured" from `require()` rather than an obscure failure.
    """
    return _CURRENT.get() or ApiServices()


@contextmanager
def bind_services(services: ApiServices) -> Iterator[None]:
    """Bind *services* for a block. For calling a route handler directly."""
    token = _CURRENT.set(services)
    try:
        yield
    finally:
        _CURRENT.reset(token)
