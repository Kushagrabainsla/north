"""WriteFileTool - write or overwrite a file in the workspace.

Gated exactly like patch_file: the tool reports what it is about to write and
where, and `approval/policy.py` decides whether that runs, asks, or is refused.
It used to write without asking at all, which made it the one way to change a
file that no mode, allowlist, or card ever saw.

See docs/CODING_STYLE.md Sections 7.3 and 16.1.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

from approval.policy import Action, ActionKind
from tools._path import is_north_scratch, resolve_path, scope_refusal
from tools.base import ApprovalGatedTool
from tools.models import ToolInput, ToolOutput
from tools.specialized._approval import gate_action
from tools.specialized.patch_file import unified_diff
from utils.text import normalize_dashes, should_normalize_prose

if TYPE_CHECKING:
    from approval.base import Notifier
    from approval.policy import ApprovalPolicy
    from approval.store import ApprovalStore
    from utils.events import EventEmitter


class WriteFileTool(ApprovalGatedTool):
    """Writes content to a file, creating parent directories as needed."""

    name = "write_file"
    is_mutating = True
    description = (
        "Create a new file, or completely OVERWRITE an existing one, with the given "
        "content (parent directories are created automatically). This replaces the whole "
        "file - it does not append or merge - so always pass the ENTIRE intended content, "
        "never a fragment. Use it for brand-new files or a deliberate full rewrite; to "
        "change part of an existing file, prefer patch_file, which edits in place without "
        "clobbering the rest. The path must be inside the workspace."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Destination file path, e.g. 'src/utils/helpers.py'"},
            "content": {
                "type": "string",
                "description": "The complete text content of the file (replaces the entire file)",
            },
            "workspace": {"type": "string", "description": "Workspace root (optional)"},
        },
        "required": ["path", "content"],
    }

    def __init__(
        self,
        approval_store: ApprovalStore | None = None,
        stream_manager: EventEmitter | None = None,
        approval_timeout_seconds: float = 300.0,
        policy: ApprovalPolicy | None = None,
        notifier: Notifier | None = None,
    ) -> None:
        super().__init__(approval_store, stream_manager, approval_timeout_seconds, policy, notifier)

    def format_output(self, data: dict[str, Any]) -> str:
        return f"Created `{data.get('path', '?')}` ({data.get('bytes_written', 0)} bytes written)."

    async def run(self, input: ToolInput) -> ToolOutput:
        path_str = input.params.get("path")
        content = input.params.get("content")
        if not path_str:
            return ToolOutput(success=False, error="Parameter 'path' is required.")
        if content is None:
            return ToolOutput(success=False, error="Parameter 'content' is required.")

        resolved = resolve_path(path_str, input.params.get("workspace"))
        if resolved is None:
            return ToolOutput(success=False, error="Path escapes workspace root.")

        # Server-owned edit-scope check, before any bytes are written. The scope
        # arrives on input.edit_scope (never params), so the model cannot widen
        # its own permissions. A None scope preserves prior unrestricted behavior.
        if (refusal := scope_refusal(input.edit_scope, resolved)) is not None:
            return ToolOutput(success=False, error=refusal, failure_kind="refused")

        # Strip em/en dashes from prose north writes (reports, notes, briefings), but
        # never from code/data files, whose dashes may be literal, test-verified bytes.
        if should_normalize_prose(resolved):
            content = normalize_dashes(content)

        # Same wiring as patch_file: the instance app.py registers carries the
        # approval store; an auto-discovered one without it is replaced at startup.
        if self._approval_store is not None:
            refused = await self._gate(input, resolved, content)
            if refused is not None:
                return refused

        return await asyncio.to_thread(_write_sync, resolved, content)

    async def _gate(self, input: ToolInput, path: Path, content: str) -> ToolOutput | None:
        """``None`` when the file may be written; otherwise what to return instead."""
        old = await asyncio.to_thread(_read_existing, path)
        return await gate_action(
            Action(
                agent="write_file",
                kind=ActionKind.FILE_EDIT,
                summary=f"write {path}",
                path=path,
                # The folder the server granted - never the model's `workspace` param.
                workspace=input.granted_workspace or "",
                in_north_scratch=is_north_scratch(path),
            ),
            policy=self._policy,
            approval_store=self._approval_store,
            title="File Write - Approval Required",
            message=f"Write `{path}`?\n```diff\n{unified_diff(path, old, content)}\n```",
            options=("Write", "Cancel"),
            task_id=input.params.get("task_id"),
            stream_manager=self._stream_manager,
            notifier=self._notifier,
            timeout=self._approval_timeout_seconds,
            declined="Write cancelled by user.",
        )


def _read_existing(path: Path) -> str:
    """The file's current text, so the card shows what the write replaces."""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _write_sync(path: Path, content: str) -> ToolOutput:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return ToolOutput(
            success=True,
            data={"path": str(path), "bytes_written": len(content.encode("utf-8"))},
        )
    except Exception as exc:
        return ToolOutput(success=False, error=str(exc))
