"""Orchestrator-level constants."""

from __future__ import annotations

import re

# Prevents runaway webhook integrations or buggy clients from burning API credits.
MAX_CONCURRENT_TASKS = 10

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
    r"(eco|cruise|sport)\s*(?:mode|strategy)?$",
    re.IGNORECASE,
)
