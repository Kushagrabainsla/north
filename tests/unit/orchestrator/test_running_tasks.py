"""Unit tests for the durable in-flight registry (orchestrator/running_tasks.py)."""

from __future__ import annotations

import asyncio

import pytest

from ledger.models import LedgerSource
from orchestrator.models import TaskRequest
from orchestrator.running_tasks import RunningTaskStore


@pytest.fixture
def store(tmp_path) -> RunningTaskStore:
    return RunningTaskStore(tmp_path / "running_tasks.db")


async def test_mark_and_list_roundtrip_full_request(store: RunningTaskStore) -> None:
    req = TaskRequest(prompt="do x", source=LedgerSource.CRON, workspace="/ws", context="ctx")
    await store.mark_running("t1", req)

    tasks = await store.list_all()
    assert len(tasks) == 1
    rt = tasks[0]
    assert rt.task_id == "t1"
    assert rt.attempt == 0
    # The whole request round-trips - the ledger alone could not do this.
    assert rt.request.prompt == "do x"
    assert rt.request.source == LedgerSource.CRON
    assert rt.request.workspace == "/ws"
    assert rt.request.context == "ctx"


async def test_mark_running_preserves_started_at_and_updates_attempt(store: RunningTaskStore) -> None:
    req = TaskRequest(prompt="p", source=LedgerSource.PROMPT)
    await store.mark_running("t1", req)
    started = (await store.list_all())[0].started_at

    await asyncio.sleep(0.01)
    await store.mark_running("t1", req, attempt=2)

    rt = (await store.list_all())[0]
    assert rt.attempt == 2
    assert rt.started_at == started  # started_at is set once, on first insert


async def test_heartbeat_advances_only_heartbeat(store: RunningTaskStore) -> None:
    req = TaskRequest(prompt="p", source=LedgerSource.PROMPT)
    await store.mark_running("t1", req)
    before = (await store.list_all())[0]

    await asyncio.sleep(0.01)
    await store.heartbeat("t1")

    after = (await store.list_all())[0]
    assert after.heartbeat_at > before.heartbeat_at
    assert after.started_at == before.started_at


async def test_clear_removes_task(store: RunningTaskStore) -> None:
    await store.mark_running("t1", TaskRequest(prompt="p", source=LedgerSource.PROMPT))
    await store.clear("t1")
    assert await store.list_all() == []


async def test_heartbeat_and_clear_missing_are_noops(store: RunningTaskStore) -> None:
    await store.heartbeat("ghost")  # must not raise
    await store.clear("ghost")  # must not raise
    assert await store.list_all() == []


async def test_list_all_orders_oldest_first(store: RunningTaskStore) -> None:
    req = TaskRequest(prompt="p", source=LedgerSource.PROMPT)
    await store.mark_running("first", req)
    await asyncio.sleep(0.01)
    await store.mark_running("second", req)
    assert [rt.task_id for rt in await store.list_all()] == ["first", "second"]


async def test_default_task_has_no_side_effects(store: RunningTaskStore) -> None:
    await store.mark_running("t1", TaskRequest(prompt="p", source=LedgerSource.PROMPT))
    assert (await store.list_all())[0].has_side_effects is False


async def test_mark_side_effect_sets_flag(store: RunningTaskStore) -> None:
    await store.mark_running("t1", TaskRequest(prompt="p", source=LedgerSource.PROMPT))
    await store.mark_side_effect("t1")
    assert (await store.list_all())[0].has_side_effects is True


async def test_side_effect_flag_preserved_across_resume_remark(store: RunningTaskStore) -> None:
    req = TaskRequest(prompt="p", source=LedgerSource.PROMPT)
    await store.mark_running("t1", req)
    await store.mark_side_effect("t1")
    await store.mark_running("t1", req, attempt=1)  # resume re-marks the row
    assert (await store.list_all())[0].has_side_effects is True


async def test_default_task_status_is_running(store: RunningTaskStore) -> None:
    await store.mark_running("t1", TaskRequest(prompt="p", source=LedgerSource.PROMPT))
    assert (await store.list_all())[0].status == "running"


async def test_mark_paused_sets_status(store: RunningTaskStore) -> None:
    await store.mark_running("t1", TaskRequest(prompt="p", source=LedgerSource.PROMPT))
    assert await store.mark_paused("t1") is True
    rt = (await store.list_all())[0]
    assert rt.status == "paused"


async def test_mark_paused_returns_false_for_unknown(store: RunningTaskStore) -> None:
    assert await store.mark_paused("ghost") is False


async def test_mark_running_from_paused_restores_status(store: RunningTaskStore) -> None:
    req = TaskRequest(prompt="p", source=LedgerSource.PROMPT)
    await store.mark_running("t1", req)
    await store.mark_paused("t1")
    assert await store.mark_running_from_paused("t1") is True
    rt = (await store.list_all())[0]
    assert rt.status == "running"


async def test_mark_running_from_paused_returns_false_for_unknown(store: RunningTaskStore) -> None:
    assert await store.mark_running_from_paused("ghost") is False


async def test_mark_running_from_paused_returns_false_if_not_paused(store: RunningTaskStore) -> None:
    await store.mark_running("t1", TaskRequest(prompt="p", source=LedgerSource.PROMPT))
    assert await store.mark_running_from_paused("t1") is False  # status is "running", not "paused"


async def test_paused_status_preserved_across_remark(store: RunningTaskStore) -> None:
    req = TaskRequest(prompt="p", source=LedgerSource.PROMPT)
    await store.mark_running("t1", req)
    await store.mark_paused("t1")
    # Re-marking with mark_running should reset to running
    await store.mark_running("t1", req, attempt=1)
    assert (await store.list_all())[0].status == "running"


async def test_get_existing_and_missing_task(store: RunningTaskStore) -> None:
    req = TaskRequest(prompt="find me", source=LedgerSource.PROMPT)
    await store.mark_running("t_point", req, attempt=3)

    found = await store.get("t_point")
    assert found is not None
    assert found.task_id == "t_point"
    assert found.attempt == 3
    assert found.request.prompt == "find me"

    assert await store.get("nonexistent") is None
