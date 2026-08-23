"""Unit tests for TUI /queue and /cancel slash commands."""

from __future__ import annotations

import contextlib
import pytest
from types import SimpleNamespace

from cli.tui import NorthApp

_DEAD = "http://127.0.0.1:8000"
_HEADERS = {"X-North-Secret": "x"}


class _FakeResp:
    def __init__(self, js, status_code: int = 200):
        self._js = js
        self.status_code = status_code

    def json(self):
        return self._js


class _CapturingClient:
    def __init__(self, calls: list[dict]):
        self.calls = calls

    async def get(self, url, **kw):
        self.calls.append({"method": "GET", "url": url, "params": kw.get("params")})
        if "/tasks" in url:
            return _FakeResp([{"task_id": "task_123", "prompt": "test prompt"}])
        if "/jobs" in url:
            return _FakeResp([{"job_id": "job_456", "task": "test job"}])
        return _FakeResp([])

    async def post(self, url, **kw):
        self.calls.append({"method": "POST", "url": url, "json": kw.get("json")})
        if "cancel-all" in url:
            return _FakeResp({"tasks_cancelled": 2, "jobs_cancelled": 1})
        if "cancel/" in url:
            target = url.split("cancel/")[-1]
            return _FakeResp({"cancelled": "task", "id": target})
        return _FakeResp({})


def _install_capturing_http(app: NorthApp) -> list[dict]:
    calls: list[dict] = []

    @contextlib.asynccontextmanager
    async def fake_http():
        yield _CapturingClient(calls)

    app._http = fake_http  # type: ignore[method-assign]
    return calls


@pytest.mark.asyncio
async def test_tui_queue_slash_command():
    app = NorthApp(base_url=_DEAD, headers=_HEADERS)
    calls = _install_capturing_http(app)

    async with app.run_test() as pilot:
        await app._handle_slash("/queue")
        # Verify both active tasks and pending jobs were requested
        urls = [c["url"] for c in calls]
        assert any("/tasks" in u for u in urls)
        assert any("/jobs" in u for u in urls)


@pytest.mark.asyncio
async def test_tui_cancel_specific_task_command():
    app = NorthApp(base_url=_DEAD, headers=_HEADERS)
    calls = _install_capturing_http(app)

    async with app.run_test() as pilot:
        await app._handle_slash("/cancel task_123")
        post_calls = [c for c in calls if c["method"] == "POST"]
        assert any("cancel/task_123" in c["url"] for c in post_calls)


@pytest.mark.asyncio
async def test_tui_cancel_all_command():
    app = NorthApp(base_url=_DEAD, headers=_HEADERS)
    calls = _install_capturing_http(app)

    async with app.run_test() as pilot:
        await app._handle_slash("/cancel all")
        post_calls = [c for c in calls if c["method"] == "POST"]
        assert any("cancel-all" in c["url"] for c in post_calls)
