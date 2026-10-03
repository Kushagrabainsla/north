"""Orchestrator-level constants."""

from __future__ import annotations

import re

# Prevents runaway webhook integrations or buggy clients from burning API credits.
MAX_CONCURRENT_TASKS = 10

# How many times an interrupted task may be re-run on startup before it is
# treated as a poison pill and marked FAILED. Guards against a task that crashes
# the server from resuming into an infinite restart loop.
MAX_RESUME_ATTEMPTS = 3

# The longest a task waiting on a missing model or network waits between tries.
# It never gives up (CODING_STYLE §13.5, #34); this only keeps a model that is
# down all day from filling the ledger with a retry a minute.
MAX_QUEUE_BACKOFF_SECONDS = 900.0

# Poll interval for draining queued tasks when waiting for model recovery.
QUEUE_POLL_INTERVAL_SECONDS = 3.0

# How often the stuck-task watchdog scans the running-task registry for tasks
# whose heartbeat has gone stale past settings.stuck_task_max_age_seconds.
WATCHDOG_POLL_INTERVAL_SECONDS = 600

# Below this confidence the north star check is skipped to avoid interrupting
# the user on borderline-classified tasks (e.g. "schedule a reminder").
NORTH_STAR_CONFIDENCE_THRESHOLD = 0.7

# Minimum seconds between reactive pool refreshes triggered by agent failures  -
# prevents refresh storms when many agents fail concurrently.
POOL_REFRESH_COOLDOWN = 60.0

# Matches an unambiguous strategy directive so incidental mentions
# ("I was in sport mode") never accidentally mutate the running strategy.
STRATEGY_CMD_RE = re.compile(
    r"^(?:(?:set|switch|use|change|enable|activate)\s+(?:to\s+)?)?(?:the\s+)?"
    r"(eco|cruise|sport)\s*(?:mode|power|strategy|autonomy)?$",
    re.IGNORECASE,
)
