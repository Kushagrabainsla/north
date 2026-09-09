"""The server actually starts.

This is the gap that let a broken build ship. Every other test constructs the
pieces it needs directly, so nothing ever ran `lifespan` - and the wiring is
exactly where the pieces meet. `import orchestrator.app` succeeds even when the
call inside it is wrong, which is what happened: three new components were
handed to `configure_web()` before its signature accepted them, and the failure
only appeared when someone ran `north update`.

Worse, the CLI crashed while *reporting* that failure (`Console.print(err=...)`),
so the message said nothing about the real cause.

The test is deliberately end-to-end and dumb: run the lifespan the way the
server does, and assert the wiring came out whole.
"""

from __future__ import annotations

import pytest

from orchestrator.api_context import services_of

# The components the lifespan is responsible for constructing and handing on.
# Adding one here is the cheap way to make sure it is actually reachable, rather
# than assigned to `deps` and never passed along.
WIRED_COMPONENTS = (
    "orchestrator",
    "approval_store",
    "unattended_rules",
    "card_continuations",
    "decision_log",
    "approval_memory",
    "cron_store",
    "agent_registry",
    "ledger",
)


@pytest.fixture
async def booted_app(monkeypatch):
    """The real app, through the real lifespan, against a throwaway home.

    One boot for the whole module. Booting per test meant eleven full startups -
    catalog refresh, embedding model, recovery sweep - for eleven read-only
    assertions about the same wiring, which took minutes. Nothing here mutates
    the app, so they can share it.
    """
    monkeypatch.setenv("NORTH_ENV", "test")
    from orchestrator.app import app

    async with app.router.lifespan_context(app):
        yield app


@pytest.mark.asyncio
async def test_the_server_starts_with_every_component_wired(booted_app) -> None:
    """If this fails, `north start` and `north update` are broken for everyone.

    Constructed in the lifespan is not the same as reachable from a request: a
    component can be built, assigned to `deps`, and still never arrive, because
    the keyword that carries it has to exist on the function that takes it. That
    is exactly how the server came to die on boot.
    """
    services = services_of(booted_app)
    assert services is not None
    missing = [name for name in WIRED_COMPONENTS if getattr(services, name, None) is None]
    assert not missing, f"built in the lifespan but never wired through: {missing}"


@pytest.mark.asyncio
async def test_a_read_through_the_wiring_answers(booted_app) -> None:
    """One real request, rather than trusting the wiring looks right."""
    from orchestrator.api_context import bind_services
    from web import api as web_api

    with bind_services(services_of(booted_app)):
        assert await web_api.approvals() == []
        assert (await web_api.unattended_rules())["rules"], "the shipped safe-action rules must be seeded"
