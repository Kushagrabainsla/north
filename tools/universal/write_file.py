"""WriteFileTool - write or overwrite a file in the workspace.

Gated exactly like patch_file: the tool reports what it is about to write and
where, and `approval/policy.py` decides whether that runs, asks, or is refused.
It used to write without asking at all, which made it the one way to change a
file that no mode, allowlist, or card ever saw.

See docs/CODING_STYLE.md Sections 7.3 and 16.1.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from approval.approvals import Request
from approval.policy import Action, ActionKind
from tools._path import is_north_scratch, resolve_path, scope_refusal
from tools.base import Tool, prepared
from tools.models import ToolInput, ToolOutput
from tools.specialized.patch_file import unified_diff
from utils.text import normalize_dashes, should_normalize_prose


@dataclass(frozen=True)
class _WriteCall:
    """A checked write, ready to describe or run."""

    path: Path
    content: str


class WriteFileTool(Tool):
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

    def format_output(self, data: dict[str, Any]) -> str:
        return f"Created `{data.get('path', '?')}` ({data.get('bytes_written', 0)} bytes written)."

    async def describe(self, input: ToolInput) -> Request | None:
        call = self._prepare(input)
        if isinstance(call, ToolOutput):
            return None
        old = await asyncio.to_thread(_read_existing, call.path)
        return Request(
            action=Action(
                agent="write_file",
                kind=ActionKind.FILE_EDIT,
                summary=f"write {call.path}",
                path=call.path,
                # The folder the server granted - never the model's `workspace` param.
                workspace=input.granted_workspace or "",
                in_north_scratch=is_north_scratch(call.path),
            ),
            title="File Write - Approval Required",
            message=f"Write `{call.path}`?\n```diff\n{unified_diff(call.path, old, call.content)}\n```",
            options=("Write", "Cancel"),
            declined="Write cancelled by user.",
            prepared=call,
        )

    async def run(self, input: ToolInput) -> ToolOutput:
        call = prepared(input) or self._prepare(input)
        if isinstance(call, ToolOutput):
            return call
        return await asyncio.to_thread(_write_sync, call.path, call.content)

    @staticmethod
    def _prepare(input: ToolInput) -> _WriteCall | ToolOutput:
        """Check the call and settle what will be written, or say why it cannot be."""
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
        return _WriteCall(resolved, content)


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
