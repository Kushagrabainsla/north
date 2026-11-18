"""North starts with any inference router, and a live test says so when it cannot (issue #82, item 6).

Startup called `set_model_sink` on the router it built, which only `ModelDispatcher` had. The
live Telegram test's scripted router failed startup with AttributeError from 2026-10-29, and
the test's wait loop hid it as a ten-minute hang, every night.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import config.dependencies
from inference.base import InferenceRouter
from tests.conftest import MockInferenceRouter
from tests.live.servers import until_started


def test_production_dependencies_build_with_a_router_that_is_not_the_dispatcher(monkeypatch) -> None:
    router = MockInferenceRouter()
    monkeypatch.setattr(config.dependencies, "build_inference_router_from_settings", lambda **_: router)

    deps = config.dependencies.build_production_dependencies()

    assert deps is not None


def test_every_router_takes_a_model_sink() -> None:
    assert "set_model_sink" in vars(InferenceRouter)
    MockInferenceRouter().set_model_sink(lambda task_id, model_id: None)


async def test_a_server_that_stops_during_startup_fails_at_once() -> None:
    async def stops() -> None:
        return None  # uvicorn returns like this when the app's startup raised

    serving = asyncio.create_task(stops())
    await asyncio.sleep(0)

    with pytest.raises(pytest.fail.Exception, match="stopped during startup"):
        await asyncio.wait_for(until_started(SimpleNamespace(started=False), serving), timeout=5)


async def test_a_server_that_never_starts_fails_at_the_limit() -> None:
    serving = asyncio.create_task(asyncio.sleep(60))

    with pytest.raises(pytest.fail.Exception, match="did not start within"):
        await until_started(SimpleNamespace(started=False), serving, timeout=0.2)
    assert serving.cancelled() or serving.cancelling()
