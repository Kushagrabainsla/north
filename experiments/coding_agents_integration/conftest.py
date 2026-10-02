"""Fixtures for the coding-agent integration experiments.

Reuses the suite's own NORTH_HOME isolation so nothing here touches ~/.north.
"""

from __future__ import annotations

import pytest

from tests.conftest import _isolated_north_home  # noqa: F401  (autouse fixture, re-exported)


@pytest.fixture
async def booted_app(monkeypatch):
    """The real app through its real lifespan, against a throwaway home."""
    monkeypatch.setenv("NORTH_ENV", "test")
    from config.settings import settings

    monkeypatch.setattr(settings, "north_env", "test")
    monkeypatch.setattr(settings, "autonomous_background_tasks_enabled", None)
    monkeypatch.setattr(settings, "onboarding_enabled", None)
    from orchestrator.app import app

    async with app.router.lifespan_context(app):
        yield app
