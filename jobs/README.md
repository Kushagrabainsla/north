# jobs

The `jobs` package is the persistent job queue and the cron scheduler of north.
A job is one unit of background work: a schedule firing, or a one-off run that a person or a tool queued.
The scheduler puts a job in the queue when a schedule comes due, and the processor runs each due job.
Both use one SQLite file, `jobs.db`, in north's home.

## Main parts

| File | Key names | What it does |
|---|---|---|
| `base.py` | `JobProcessor` (ABC) | The queue interface: `enqueue`, `get`, `claim_next`, `mark_completed`, `mark_failed`, `cancel`, `list_jobs`, `has_active_firing`, `first_firing_epoch`, `run`. |
| `sqlite_processor.py` | `SQLiteJobProcessor` | The SQLite queue (table `job_queue`). Polls, runs at most `_MAX_CONCURRENT_JOBS` jobs at a time, retries a failed job with backoff (30 s, doubled, capped at 1 h), and requeues jobs left in `running` by a dead process (`reap_stale_running`). |
| `scheduler.py` | `CronEntry`, `CronScheduler`, `next_firing`, `previous_firing`, `next_due_entry`, `SYSTEM_CRON_ENTRIES`, `V1_CRON_ENTRIES`, `merge_entries`, `provision_default_schedules` | Schedule rules and the scheduler loop. One asyncio task sleeps until the next firing (at most 60 s), enqueues the job, and repeats. No external scheduling library. |
| `cron_store.py` | `UserCronStore`, `schedule_name`, `UNSET` | Stores the user's schedules and edits of built-in schedules (tables `user_cron_entries`, `schedule_provisioning`, `schedule_first_seen`). |
| `models.py` | `Job`, `JobType`, `JobStatus`, `JobPriority`, `REMINDER_FLOW`, `late_note`, `display_job_status` | The job model and its enums. `late_note` says how late a run started and why. |
| `seen.py` | `SeenStore`, `normalize_key` | Remembers which candidates a source already offered, so a daily flow does not offer them again (table `seen`). |
| `exceptions.py` | `JobError`, `JobNotFoundError`, `JobProcessingError`, `JobNeedsAttention`, `JobNeverStarted`, `JobCancelled` | Queue errors. The job handler raises the last three to tell the processor how a job ended. |

### Schedules

- A `CronEntry` is a wall-clock rule (`hour`, `minute`, `weekdays`, IANA `tz`) or a fixed interval (`interval_minutes` plus `anchor_epoch`).
- `SYSTEM_CRON_ENTRIES` are north's own upkeep schedules. Each runs one built-in flow, for example `task_context_cleanup` runs the `nightly-cleanup` flow at 03:00.
- A stored row with the same name replaces a built-in (`merge_entries`). If you delete the row, the shipped default comes back.
- `PROVISIONED_CRON_ENTRIES` are seeded once into the user's store. The list is empty today.
- At startup the scheduler catches up a firing that north missed in the last 24 hours (`_STARTUP_CATCHUP`).
- A firing has one job id (`_firing_id` of name and slot). A slot that already ran, or is still running, does not run again.
- A late job records why in `payload["late_because"]`: `ASLEEP` or `NORTH_WAS_OFF`.

## How north uses it

- `config/dependencies.py` builds `SQLiteJobProcessor` and `UserCronStore` on `~/.north/jobs.db`.
- `orchestrator/app.py` builds `CronScheduler(processor, V1_CRON_ENTRIES, cron_store)` and starts it and `job_processor.run(on_job=_dispatch_job)` as background loops.
- `_dispatch_job` in `orchestrator/app.py` runs the job's flow through the `run_flow` tool, or starts a scheduled task. It raises `JobNeverStarted` or `JobNeedsAttention` when a flow fails.
- `orchestrator/app.py` calls `provision_default_schedules` before the scheduler starts, and gives `SeenStore` to the agents as `offered_store` and to `tools/universal/offered_before.py`.
- `orchestrator/api/jobs.py` and `orchestrator/api/cron.py` serve the queue and the schedules over REST. `orchestrator/api/health.py` reports `broken_schedules`.
- `tools/universal/_schedules.py`, `tools/universal/flow_schedules.py` and `web/extensions.py` read and write schedules and jobs.
- The public interface is `jobs/__init__.py` `__all__`. `SeenStore`, `JobNeverStarted`, `merge_entries` and `provision_default_schedules` are not in it. Callers import them from the submodule.

## Rules

- Depend on `JobProcessor`, not on `SQLiteJobProcessor` (CODING_STYLE §6.1, §2.5).
- Store every instant as a Unix epoch `REAL`. A schedule stores wall-clock time plus an IANA zone, so it stays fixed across a DST shift (CODING_STYLE §5.2.1).
- Each store creates and changes its own schema with `CREATE TABLE IF NOT EXISTS` and guarded `ALTER TABLE` (CODING_STYLE §11.4). Use `open_db_connection` from `utils/db.py` (CODING_STYLE §11.1).
- Do not retry a job automatically when a retry can apply a change twice. Set `max_retries` to 0 or raise `JobNeedsAttention`. The job then waits in `needs_attention` for a person.
- A missed slot runs once, not once for each restart. Keep job ids from `_firing_id`, and keep the `_already_fired` check.
- A built-in schedule always takes its description, agent, task, skill and flow from the shipped entry (`apply_shipped_defaults`). A user edit cannot change what a built-in runs.
- The scheduler and the processor run as loops in the FastAPI lifespan (CODING_STYLE §10.4).

## Data on disk

- `~/.north/jobs.db`: tables `job_queue`, `user_cron_entries`, `schedule_provisioning`, `schedule_first_seen` and `seen`.
