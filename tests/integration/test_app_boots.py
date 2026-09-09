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
    """The real app, through the real lifespan, against a throwaway home."""
    monkeypatch.setenv("NORTH_ENV", "test")
    from orchestrator.app import app

    async with app.router.lifespan_context(app):
        yield app


@pytest.mark.asyncio
async def test_the_server_starts(booted_app) -> None:
    """If this fails, `north start` and `north update` are broken for everyone."""
    assert services_of(booted_app) is not None


@pytest.mark.parametrize("component", WIRED_COMPONENTS)
@pytest.mark.asyncio
async def test_every_component_reaches_the_api_layer(booted_app, component: str) -> None:
    """Constructed in the lifespan is not the same as reachable from a request.

    A component can be built, assigned to `deps`, and still never arrive - the
    keyword that carries it has to exist on the function that takes it.
    """
    assert getattr(services_of(booted_app), component, None) is not None, (
        f"{component} was not wired through to the API layer"
    )


@pytest.mark.asyncio
async def test_the_approvals_endpoint_answers(booted_app) -> None:
    """One real read through the wiring, rather than trusting it looks right."""
    from orchestrator.api_context import bind_services
    from web import api as web_api

    with bind_services(services_of(booted_app)):
        assert await web_api.approvals() == []
        assert (await web_api.unattended_rules())["rules"], "the shipped safe-action rules must be seeded"
