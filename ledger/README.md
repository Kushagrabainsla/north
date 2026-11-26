# ledger

The `ledger` package is the audit trail of north. Each notable event (a task step, an agent
result, a model call, an approval, an error) writes one `LedgerEntry`. North reads the ledger to
show task history and costs, to learn memory from finished tasks, and to keep the final answer of
old sessions. `SQLiteLedgerWriter` keeps all entries in one SQLite file, `ledger.db`.

## Main parts

| File | Key names | What it does |
|---|---|---|
| `base.py` | `LedgerWriter` (ABC), `LedgerFilters`, `SearchResult` | The interface. `write`, `get` and `query` are abstract. `query_summaries`, `search`, `cost_breakdown`, `component_cost`, `pending_task_ids`, `prune` and `get_metrics` have defaults that work in Python, so a fake store needs only the three abstract methods. |
| `models.py` | `LedgerEntry`, `LedgerSummary`, `LedgerSource`, `LedgerStatus`, `TASK_ENDINGS`, `INFERENCE_CALL` | The entry model and its enums. `LedgerEntry.new()` sets the id, the UTC timestamp and the run identity. `LedgerSummary` is a row without the large text columns. |
| `sqlite_writer.py` | `SQLiteLedgerWriter` | The SQLite implementation. Makes the schema, the indexes and an FTS5 index (`ledger_fts`) on construction. Runs each blocking call in `asyncio.to_thread`. Overrides the defaults with SQL (`GROUP BY` for costs, FTS5 for search). |
| `exceptions.py` | `LedgerError`, `LedgerWriteError`, `LedgerReadError` | Errors. The writer wraps `sqlite3.Error` in these. |

## How north uses it

- `config/dependencies.py` makes the one `SQLiteLedgerWriter` on `<north home>/ledger.db`. Its
  inference call sink writes one `INFERENCE_CALL` row (source `INFERENCE_ROUTER`) per model call.
- `orchestrator/journal.py` (`TaskJournal`) writes task events to the ledger and the live stream
  together. It tells listeners when a `TASK_ENDINGS` action is written.
- `agents/base.py`, `orchestrator/app.py`, `orchestrator/reconcile.py` and
  `approval/interaction.py` write entries.
- `memory/extraction.py` and `memory/consolidator.py` read new entries with `query()` to learn
  facts and episodes. `memory/extraction.py` reads its daily spend with `component_cost()`.
- `orchestrator/api/ledger.py` serves `GET /ledger` and `GET /ledger/search`.
  `orchestrator/api/inference.py` calls `cost_breakdown()`.
- `tools/universal/query_metrics.py` calls `get_metrics()`. `tools/universal/get_task_status.py`
  calls `query()` and `pending_task_ids()`.
- The nightly cleanup in `orchestrator/app.py` calls `prune()` with the
  `task_cleanup_completed_days` and `task_cleanup_failed_days` settings.
- Public interface: `__all__` in `ledger/__init__.py`. Some callers import `ledger.base` and
  `ledger.models` directly.

## Rules

- Import from `ledger`, not from its files (CODING_STYLE §7.1, §7.2).
- Depend on `LedgerWriter`, never on `SQLiteLedgerWriter` (CODING_STYLE §6.1).
- Keep ledger writes off the main path. Use `spawn(ledger.write(...))` from `utils/tasks.py`
  (CODING_STYLE §14.1).
- Use `LedgerSource` and `LedgerStatus`, never plain strings (CODING_STYLE §5.5, §14.2).
- Make entries with `LedgerEntry.new(source, ...)`. It fills `run_id`, `parent_run_id` and
  `attempt` from `utils/execution_context.py`.
- Add up cost and tokens from `INFERENCE_CALL` rows only. Agent and task rows repeat the cost of
  their calls, so a sum over all rows counts a call two or three times.
- The status of a task is its most recent `TASK_ENDINGS` action, not its most recent entry.
- `cached_tokens=None` means the provider did not report caching. It does not mean a cache miss.
- Do not change or delete rows. Only `prune()` deletes. It keeps the newest non-empty
  `agent_completed` row and the newest task-ending row of each task.
- The writer owns its schema. Add a column as an `ALTER TABLE` line in `_MIGRATIONS`
  (CODING_STYLE §11.4). Open connections with `open_db_connection` and use `?` placeholders
  (CODING_STYLE §11.1, §11.2).
- Wrap `sqlite3` errors in `LedgerWriteError` or `LedgerReadError` (CODING_STYLE §13.1, §13.4).
- Add tests in `tests/unit/ledger/` with the change (CODING_STYLE §18.8).

## Data on disk

- `<north home>/ledger.db` (`~/.north/ledger.db` by default): the `ledger` table and the
  `ledger_fts` FTS5 table, kept in step by triggers.
- `timestamp` is ISO-8601 text from `datetime.isoformat()`. `agent_output` and `tools_used` are
  JSON text.
- If SQLite has no FTS5, the writer skips the index. Then `search()` uses the slower text scan in
  `LedgerWriter.search`, which has no ranking.
