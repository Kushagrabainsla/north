"""#26 before/after: which tasks can the dashboard's Tasks page reach, and find with its search?

Usage: e26_task_history.py

Works on a copy of north's ledger (`~/.north/ledger.db`, copied with SQLite's backup API, so the real
file is only read). Before: the page loaded the latest 500 entries and searched them in the browser.
After: it loads whole tasks (100, then 100 more per "Show more") and searches on the server.

The searches are the first two words of the prompt of each task outside the latest 500 entries, as a
person would type them. The results hold counts and timings only, never a prompt.
"""

from __future__ import annotations

import asyncio
import json
import random
import sqlite3
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ledger.base import LedgerFilters  # noqa: E402
from ledger.sqlite_writer import SQLiteLedgerWriter  # noqa: E402

SOURCE = Path.home() / ".north" / "ledger.db"
RESULTS = Path(__file__).resolve().parent / "results"
PAGE = 100
SAMPLE = 50


def copy_ledger() -> Path:
    target = Path(tempfile.mkdtemp(prefix="north-e26-")) / "ledger.db"
    with sqlite3.connect(f"file:{SOURCE}?mode=ro", uri=True) as source, sqlite3.connect(target) as copy:
        source.backup(copy)
    return target


def prompts(entries) -> dict[str, str]:
    """Each task's prompt, from its task_received entry."""
    return {e.task_id: e.input for e in entries if e.task_id and e.action == "task_received" and e.input}


async def main() -> None:
    store = SQLiteLedgerWriter(copy_ledger())
    everything = await store.query(LedgerFilters(limit=1_000_000))
    all_tasks = {e.task_id for e in everything if e.task_id}
    latest = await store.query(LedgerFilters(limit=500))
    before_tasks = {e.task_id for e in latest if e.task_id}

    shown, clicks, after_tasks = PAGE, 0, set()
    while True:
        after_tasks = {e.task_id for e in await store.task_events(shown) if e.task_id}
        if len(after_tasks) < shown:
            break
        shown += PAGE
        clicks += 1

    older = {task: text for task, text in prompts(everything).items() if task not in before_tasks}
    random.seed(0)
    picked = random.sample(sorted(older), min(SAMPLE, len(older)))
    found_before = found_after = found_any = 0
    timings = []
    for task in picked:
        words = " ".join(older[task].split()[:2])
        lowered = words.lower()
        if any(task == e.task_id and lowered in json.dumps(e.model_dump(mode="json")).lower() for e in latest):
            found_before += 1
        started = time.perf_counter()
        hits = await store.task_events(PAGE, words)
        timings.append(time.perf_counter() - started)
        found_after += task in {e.task_id for e in hits}
        found_any += task in {e.task_id for e in await store.task_events(len(all_tasks), words)}
    started = time.perf_counter()
    await store.task_events(PAGE)
    first_page = time.perf_counter() - started

    result = {
        "tasks_in_ledger": len(all_tasks),
        "before_tasks_the_page_reaches": len(before_tasks),
        "after_tasks_the_page_reaches": len(after_tasks),
        "after_show_more_clicks_to_reach_all": clicks,
        "older_tasks_searched": len(picked),
        "before_found_by_search": found_before,
        "after_found_by_search": found_after,
        "after_found_by_search_with_show_more": found_any,
        "after_first_page_ms": round(first_page * 1000, 1),
        "after_search_ms_median": round(statistics.median(timings) * 1000, 1) if timings else None,
    }
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "e26_task_history.json").write_text(json.dumps(result, indent=1) + "\n")
    print(json.dumps(result, indent=1))


if __name__ == "__main__":
    asyncio.run(main())
