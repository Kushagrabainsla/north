"""A run moved into a worktree is granted the worktree in its workspace's place."""

from __future__ import annotations

from agents.models import AgentPayload
from orchestrator.isolation import _in_worktree


def test_the_grant_moves_with_the_run() -> None:
    payload = AgentPayload(task_id="t", prompt="p", workspace="/repo", granted_workspace="/repo")

    assert _in_worktree(payload, "/wt/coder-t") == {"workspace": "/wt/coder-t", "granted_workspace": "/wt/coder-t"}


def test_a_run_granted_nothing_stays_granted_nothing() -> None:
    payload = AgentPayload(task_id="t", prompt="p", workspace="/repo")

    assert _in_worktree(payload, "/wt/coder-t")["granted_workspace"] == ""
