"""Shared utility modules.

See docs/CODING_STYLE.md Section 5.2 and Section 7.2.
"""

from __future__ import annotations

from utils.db import open_db_connection
from utils.ids import generate_id, generate_task_id
from utils.prompts import load_prompt
from utils.time import format_timestamp, utcnow

__all__ = [
    "open_db_connection",
    "generate_id",
    "generate_task_id",
    "load_prompt",
    "utcnow",
    "format_timestamp",
]
