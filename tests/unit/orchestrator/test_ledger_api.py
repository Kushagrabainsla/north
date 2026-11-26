"""`/orchestrator/ledger?tasks=N&q=...` serves whole tasks; without `tasks` the listing is as before."""

from __future__ import annotations

from orchestrator.api import ledger as ledger_api


class _Store:
    def __init__(self) -> None:
        self.asked: list[tuple] = []

    async def task_events(self, tasks: int, query: str = "") -> list:
        self.asked.append(("task_events", tasks, query))
        return []

    async def query(self, filters) -> list:
        self.asked.append(("query", filters.limit, filters.task_id))
        return []


async def test_tasks_asks_the_store_for_whole_tasks_with_the_search(monkeypatch) -> None:
    store = _Store()
    monkeypatch.setattr(ledger_api, "_get_ledger", lambda: store)

    await ledger_api.query_ledger(tasks=100, q="paneer")

    assert store.asked == [("task_events", 100, "paneer")]


async def test_without_tasks_the_listing_is_the_latest_entries(monkeypatch) -> None:
    store = _Store()
    monkeypatch.setattr(ledger_api, "_get_ledger", lambda: store)

    await ledger_api.query_ledger(limit=500, tasks=None, q="")

    assert store.asked == [("query", 500, None)]
