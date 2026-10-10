"""Exact, consent-bound edits to user-owned skill/flow documents (not source code)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path

from approval.approvals import Request
from policies.self_edit import Mutation, SelfEditPolicy
from tools.models import ToolInput


@dataclass(frozen=True)
class _Edit:
    path: str
    before: str
    params: str


def _edit(path: Path, input: ToolInput) -> _Edit:
    params = {key: value for key, value in input.params.items() if key != "task_id"}
    return _Edit(
        str(path.resolve()),
        hashlib.sha256(path.read_bytes()).hexdigest(),
        json.dumps(params, sort_keys=True, default=str),
    )


def describe_user_edit(policy: SelfEditPolicy | None, path: Path, input: ToolInput, request: Request) -> Request:
    if policy and policy.authorize(path, "update") and not policy.authorize(path, "user_update"):
        return replace(
            request,
            requires_confirmation=True,
            prepared=_edit(path, input),
            message=f"Update your custom procedure at {path.resolve()}. "
            "The previous version will be backed up.\n\n" + request.message,
        )
    return request


def begin_edit(policy: SelfEditPolicy, path: Path, operation: str, input: ToolInput) -> Mutation:
    if operation == "update" and policy.authorize(path, operation):
        request = input.approved
        if (
            isinstance(request, Request)
            and request.requires_confirmation
            and isinstance(request.prepared, _Edit)
            and request.prepared == _edit(path, input)
        ):
            return policy.begin(path, "user_update")
    return policy.begin(path, operation)
