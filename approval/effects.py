"""Side effects that must not happen twice in one task.

A flow that errors pauses at that step and resumes by running the step again
(CODING_STYLE §13.5, #33). A step that had already submitted an application or
sent a message before it failed would do it a second time. So every action that
reaches someone else or spends money is recorded once it succeeds, per task, and
the approval layer refuses the identical action later in that task: the second
time is refused with a reason, not repeated.

"Identical" is the action's identity (`Action.describe()`): the same button on
the same page, the same command. What counts as reaching someone or spending is
`forbidden_reason`, the same test that keeps these actions off the safe list.

Each run of a scheduled flow is a new task, so the same action is also recorded
for the flow (`flow:<name>`). A later run that is about to repeat it is not
refused - a flow may repeat an action on purpose - but the approval layer states
the earlier date as a fact, to you on the card and to the memory decider (issue
#82, item 4). The site had stopped a second LinkedIn invitation; another site may not.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from pathlib import Path

from utils.db import open_db_connection
from utils.secrets import redact

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS completed_effects (
    task_id    TEXT NOT NULL,
    action_key TEXT NOT NULL,
    summary    TEXT NOT NULL DEFAULT '',
    done_at    TEXT NOT NULL,
    PRIMARY KEY (task_id, action_key)
)
"""


def _digest(action_key: str) -> str:
    """What is stored for an action: it only has to match again, and its text can hold a credential."""
    return hashlib.sha256(action_key.encode()).hexdigest()


class EffectLog:
    """The irreversible actions each task has already carried out."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with open_db_connection(self._db_path) as conn:
            conn.execute(_SCHEMA)

    def done(self, task_id: str, action_key: str) -> bool:
        return self.done_at(task_id, action_key) is not None

    def done_at(self, scope: str, action_key: str) -> str | None:
        """When the action was done in *scope* (a task id, or `flow:<name>`), as UTC ISO text, or None."""
        with open_db_connection(self._db_path) as conn:
            row = conn.execute(
                "SELECT done_at FROM completed_effects WHERE task_id = ? AND action_key = ?",
                (scope, _digest(action_key)),
            ).fetchone()
        return row[0] if row is not None else None

    def record(self, task_id: str, action_key: str, summary: str = "") -> None:
        try:
            with open_db_connection(self._db_path) as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO completed_effects (task_id, action_key, summary, done_at) "
                    "VALUES (?, ?, ?, ?)",
                    (task_id, _digest(action_key), redact(summary)[:500], datetime.now(UTC).isoformat()),
                )
        except Exception:
            # The action has already happened; failing it now because the
            # bookkeeping failed would only hide that it did.
            logger.warning("EffectLog: could not record a completed action for task %s", task_id, exc_info=True)
