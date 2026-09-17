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
    from config.settings import settings

    # The settings singleton may have been imported during collection, before
    # this fixture set the environment. Make the intended test policy explicit
    # and ignore any opt-ins from the developer's trusted ~/.north/.env.
    monkeypatch.setattr(settings, "north_env", "test")
    monkeypatch.setattr(settings, "autonomous_background_tasks_enabled", None)
    monkeypatch.setattr(settings, "onboarding_enabled", None)
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


@pytest.mark.asyncio
async def test_test_mode_does_not_launch_autonomous_workers(booted_app) -> None:
    """A server wiring test must never execute schedules or scan personal files."""
    import asyncio

    from config.settings import settings

    active_names = {task.get_name() for task in asyncio.all_tasks() if not task.done()}
    assert not active_names.intersection(
        {
            "bootstrap",
            "callback_server",
            "cron_scheduler",
            "episode_consolidator",
            "fact_maintenance",
            "job_processor",
            "pool_refresh",
            "skill_distiller",
            "stuck_task_watchdog",
            "task_queue_drainer",
            "telegram_gateway",
            "tool_index",
        }
    )
    assert not (settings.north_home / ".bootstrapped").exists()
