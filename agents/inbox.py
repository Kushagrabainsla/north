"""Messages you send a task while it works (CODING_STYLE §15.4)."""

from __future__ import annotations


class TaskInbox:
    """What you sent each task that has not finished, for its agents to read.

    Kept in memory for as long as the task exists, so a task paused or queued
    for a model reads its messages when it runs again. Every message is also on
    the ledger, which is what a task that starts over after a restart opens with.
    Each agent reads from its own position, so every agent working on the task
    sees every message; ``take_unread`` hands the orchestrator those no agent read.
    """

    def __init__(self) -> None:
        self._messages: dict[str, list[str]] = {}
        self._read: dict[str, int] = {}

    def open(self, task_id: str, earlier: list[str] | None = None) -> None:
        """Start taking messages for *task_id*; *earlier* ones are known already, not new."""
        if task_id in self._messages:
            return
        self._messages[task_id] = list(earlier or [])
        self._read[task_id] = len(self._messages[task_id])

    def is_open(self, task_id: str) -> bool:
        return task_id in self._messages

    def count(self, task_id: str) -> int:
        """How many messages the task has been sent."""
        return len(self._messages.get(task_id, []))

    def post(self, task_id: str, text: str) -> bool:
        """Leave *text* for the task's agents; False when the task takes none."""
        if task_id not in self._messages:
            return False
        self._messages[task_id].append(text)
        return True

    def read(self, task_id: str, since: int) -> list[str]:
        """The messages after the first *since*, which an agent is reading now."""
        messages = self._messages.get(task_id, [])
        if len(messages) > since:
            self._read[task_id] = max(self._read[task_id], len(messages))
        return messages[since:]

    def take_unread(self, task_id: str) -> list[str]:
        """The messages no agent has read, now handed to the caller."""
        messages = self._messages.get(task_id, [])
        unread = messages[self._read.get(task_id, len(messages)) :]
        if task_id in self._read:
            self._read[task_id] = len(messages)
        return unread

    def close(self, task_id: str) -> None:
        """The task has finished: it takes no more messages."""
        self._messages.pop(task_id, None)
        self._read.pop(task_id, None)
