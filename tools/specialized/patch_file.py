"""PatchFileTool - replace exact strings in a file, with optional diff preview.

Analogous to Claude Code's Edit tool. Supports three change shapes: an ordered
`edits` list, a single `old_string`/`new_string`, or `<<<<<<< SEARCH` /
`>>>>>>> REPLACE` blocks. Every shape fails loudly if a target is missing or not
unique so the model can never silently corrupt a file.

When an ApprovalStore is injected, the computed change is shown to the user as a
unified diff and applied only on confirmation (see #15 diff-preview-before-write).
Without one (e.g. in tests), the edit applies immediately.
"""

from __future__ import annotations

import asyncio
import difflib
import re
from pathlib import Path
from typing import Any

from tools._path import resolve_path
from tools.base import ApprovalGatedTool
from tools.models import ToolInput, ToolOutput
from tools.specialized._approval import request_approval_decision

_BLOCK_RE = re.compile(r"<<<<<<< SEARCH\r?\n(.*?)\r?\n=======\r?\n(.*?)\r?\n>>>>>>> REPLACE", re.DOTALL)
_MAX_DIFF_CHARS = 8_000


class PatchFileTool(ApprovalGatedTool):
    """Replace exact strings in a file. Previews a unified diff before applying."""

    name = "patch_file"
    is_mutating = True
    description = (
        "Replace text in a file. Three ways to specify the change:\n"
        "1. edits: a list of {old_string, new_string} objects applied in order - each "
        "old_string must appear exactly once at the time it is applied. Best for "
        "renaming a symbol across several sites in one call.\n"
        "2. old_string + new_string: a single exact replacement (old_string must be unique).\n"
        "3. new_string containing SEARCH/REPLACE blocks:\n"
        "<<<<<<< SEARCH\n"
        "<exact code to find>\n"
        "=======\n"
        "<replacement code>\n"
        ">>>>>>> REPLACE"
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the file to edit"},
            "edits": {
                "type": "array",
                "description": "Ordered list of edits; each old_string must be unique when applied.",
                "items": {
                    "type": "object",
                    "properties": {
                        "old_string": {"type": "string", "description": "Exact text to find (unique)"},
                        "new_string": {"type": "string", "description": "Replacement text"},
                    },
                    "required": ["old_string", "new_string"],
                },
            },
            "old_string": {
                "type": "string",
                "description": (
                    "Exact text to find - must appear exactly once in the file."
                    " Optional if using edits or SEARCH/REPLACE blocks."
                ),
            },
            "new_string": {
                "type": "string",
                "description": "Replacement text or SEARCH/REPLACE blocks",
            },
            "workspace": {"type": "string", "description": "Workspace root (optional)"},
        },
        "required": ["path"],
    }

    def format_output(self, data: dict[str, Any]) -> str:
        return f"Patched `{data.get('path', '?')}` successfully."

    async def run(self, input: ToolInput) -> ToolOutput:
        path_str = input.params.get("path")
        edits = input.params.get("edits")
        old_string = input.params.get("old_string")
        new_string = input.params.get("new_string")

        if not path_str:
            return ToolOutput(success=False, error="Parameter 'path' is required.")
        if edits is None and new_string is None:
            return ToolOutput(success=False, error="Provide either 'edits' or 'new_string'.")

        resolved = resolve_path(path_str, input.params.get("workspace"))
        if resolved is None:
            return ToolOutput(success=False, error="Path escapes workspace root.")

        plan = await asyncio.to_thread(_plan, resolved, edits, old_string, new_string)
        if isinstance(plan, ToolOutput):
            return plan  # error
        new_content, old_content, blocks_applied = plan

        if new_content == old_content:
            return ToolOutput(success=True, data={"path": str(resolved), "blocks_applied": 0, "unchanged": True})

        if self._approval_store is not None:
            task_id = input.params.get("task_id")
            approved = await self._request_diff_approval(task_id, resolved, old_content, new_content)
            if not approved:
                return ToolOutput(success=False, error="Edit cancelled by user.")

        return await asyncio.to_thread(_write, resolved, old_content, new_content, blocks_applied)

    async def _request_diff_approval(self, task_id: str | None, path: Path, old: str, new: str) -> bool:
        diff = _unified_diff(path, old, new)
        return await request_approval_decision(
            self._approval_store,
            task_id=task_id,
            agent="patch_file",
            title="File Edit - Approval Required",
            message=f"Apply this change to `{path}`?\n```diff\n{diff}\n```",
            options=("Apply", "Cancel"),
            stream_manager=self._stream_manager,
            judgement_filter=self._judgement_filter,
            timeout=self._approval_timeout_seconds,
        )


def _unified_diff(path: Path, old: str, new: str) -> str:
    lines = difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=f"a/{path.name}",
        tofile=f"b/{path.name}",
    )
    diff = "".join(lines)
    if len(diff) > _MAX_DIFF_CHARS:
        diff = diff[:_MAX_DIFF_CHARS] + f"\n[…{len(diff) - _MAX_DIFF_CHARS} chars of diff truncated]"
    return diff


def _plan(path: Path, edits: Any, old_string: str | None, new_string: str | None) -> tuple[str, str, int] | ToolOutput:
    """Compute the would-be new file content without writing it.

    Returns (new_content, old_content, blocks_applied) or a ToolOutput on error.
    """
    if not path.exists() or not path.is_file():
        return ToolOutput(success=False, error=f"File not found: {path}")
    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return ToolOutput(success=False, error=f"Binary file cannot be patched: {path}")

    if edits is not None:
        return _plan_edits(content, edits)
    return _plan_blocks_or_legacy(content, old_string, new_string or "")


def _plan_edits(content: str, edits: Any) -> tuple[str, str, int] | ToolOutput:
    if not isinstance(edits, list) or not edits:
        return ToolOutput(success=False, error="'edits' must be a non-empty list.")
    new_content = content
    for index, edit in enumerate(edits):
        if not isinstance(edit, dict):
            return ToolOutput(success=False, error=f"Edit {index} is not an object.")
        old_string = edit.get("old_string")
        replacement = edit.get("new_string")
        if old_string is None or replacement is None:
            return ToolOutput(success=False, error=f"Edit {index} needs both 'old_string' and 'new_string'.")
        count = new_content.count(old_string)
        if count == 0:
            return ToolOutput(
                success=False,
                error=f"Edit {index}: old_string not found. Check exact whitespace and newlines.",
            )
        if count > 1:
            return ToolOutput(
                success=False,
                error=f"Edit {index}: old_string appears {count} times - add surrounding context.",
            )
        new_content = new_content.replace(old_string, replacement, 1)
    return new_content, content, len(edits)


def _plan_blocks_or_legacy(content: str, old_string: str | None, new_string: str) -> tuple[str, str, int] | ToolOutput:
    blocks = _BLOCK_RE.findall(new_string)
    if blocks:
        new_content = content
        for search_val, replace_val in blocks:
            count = new_content.count(search_val)
            if count == 0:
                return ToolOutput(
                    success=False,
                    error=f"SEARCH block not found in file:\n{search_val}\nCheck for exact spacing/newlines.",
                )
            if count > 1:
                return ToolOutput(
                    success=False,
                    error=f"SEARCH block is not unique, appears {count} times:\n{search_val}",
                )
            new_content = new_content.replace(search_val, replace_val, 1)
        return new_content, content, len(blocks)

    if old_string is None:
        return ToolOutput(
            success=False,
            error="Either old_string must be provided, or new_string must contain SEARCH/REPLACE blocks.",
        )
    count = content.count(old_string)
    if count == 0:
        return ToolOutput(
            success=False,
            error="old_string not found in file. Check for exact whitespace and newlines.",
        )
    if count > 1:
        return ToolOutput(
            success=False,
            error=(
                f"old_string appears {count} times - not unique. "
                "Add more surrounding context to make it match exactly once."
            ),
        )
    return content.replace(old_string, new_string, 1), content, 1


def _write(path: Path, old_content: str, new_content: str, blocks_applied: int) -> ToolOutput:
    try:
        path.write_text(new_content, encoding="utf-8")
    except OSError as exc:
        return ToolOutput(success=False, error=str(exc))
    return ToolOutput(
        success=True,
        data={
            "path": str(path),
            "bytes_before": len(old_content.encode("utf-8")),
            "bytes_after": len(new_content.encode("utf-8")),
            "blocks_applied": blocks_applied,
        },
    )
