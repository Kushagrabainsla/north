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
from typing import TYPE_CHECKING, Any

from approval.mode import ApprovalMode
from approval.models import ApprovalDecision
from approval.unattended import UnattendedPolicy
from tools._path import resolve_path
from tools._read_tracker import record_read, was_read
from tools.base import ApprovalGatedTool
from tools.models import ToolInput, ToolOutput
from tools.specialized._approval import refusal_output, request_approval_status
from tools.specialized._edit_match import find_unique, indents_for, reindent

if TYPE_CHECKING:
    from collections.abc import Callable

    from approval.base import Notifier
    from approval.judgement_filter import JudgementFilter
    from approval.store import ApprovalStore
    from orchestrator.stream import EventStreamManager

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

    def __init__(
        self,
        approval_store: ApprovalStore | None = None,
        stream_manager: EventStreamManager | None = None,
        approval_timeout_seconds: float = 300.0,
        judgement_filter: JudgementFilter | None = None,
        notifier: Notifier | None = None,
        unattended: UnattendedPolicy | None = None,
        mode_provider: Callable[[], ApprovalMode] | None = None,
    ) -> None:
        super().__init__(approval_store, stream_manager, approval_timeout_seconds, judgement_filter, notifier)
        self._unattended = unattended or UnattendedPolicy()
        self._mode_provider = mode_provider

    def _auto_edits(self, resolved: Path, workspace: str | None) -> bool:
        """True when the current mode auto-approves this in-workspace edit."""
        mode = self._mode_provider() if self._mode_provider is not None else ApprovalMode.INTERACTIVE
        return mode in (ApprovalMode.AUTO, ApprovalMode.AUTONOMOUS) and self._unattended.approves_edit(
            resolved, workspace
        )

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

        # Editing from memory is where exact matching goes wrong: the model
        # reproduces a block it saw several turns and two edits ago, the
        # whitespace differs, and the edit fails. Reading first is cheap and
        # makes the match reliable, so it is a precondition rather than advice.
        if not was_read(input.params.get("task_id"), str(resolved)):
            return ToolOutput(
                success=False,
                error=(
                    f"Read `{resolved}` before editing it - call read_file on it first. "
                    "Editing from memory is how old_string ends up not matching."
                ),
            )

        plan = await asyncio.to_thread(_plan, resolved, edits, old_string, new_string)
        if isinstance(plan, ToolOutput):
            return plan  # error
        new_content, old_content, blocks_applied = plan

        if new_content == old_content:
            return ToolOutput(success=True, data={"path": str(resolved), "blocks_applied": 0, "unchanged": True})

        if self._approval_store is not None and not self._auto_edits(resolved, input.params.get("workspace")):
            task_id = input.params.get("task_id")
            status = await self._request_diff_approval(task_id, resolved, old_content, new_content)
            refused = refusal_output(
                status, timeout=self._approval_timeout_seconds, declined="Edit cancelled by user."
            )
            if refused is not None:
                return refused

        written = await asyncio.to_thread(_write, resolved, old_content, new_content, blocks_applied)
        if written.success:
            record_read(input.params.get("task_id"), str(resolved))
        return written

    async def _request_diff_approval(
        self, task_id: str | None, path: Path, old: str, new: str
    ) -> ApprovalDecision:
        diff = _unified_diff(path, old, new)
        return await request_approval_status(
            self._approval_store,
            task_id=task_id,
            agent="patch_file",
            title="File Edit - Approval Required",
            message=f"Apply this change to `{path}`?\n```diff\n{diff}\n```",
            options=("Apply", "Cancel"),
            stream_manager=self._stream_manager,
            judgement_filter=self._judgement_filter,
            notifier=self._notifier,
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
        return ToolOutput(success=False, error=f"File not found: {path}", failure_kind="not_found")
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
    spans: list[tuple[int, int, str, int]] = []
    for index, edit in enumerate(edits):
        if not isinstance(edit, dict):
            return ToolOutput(success=False, error=f"Edit {index} is not an object.")
        old_string = edit.get("old_string")
        replacement = edit.get("new_string")
        if old_string is None or replacement is None:
            return ToolOutput(success=False, error=f"Edit {index} needs both 'old_string' and 'new_string'.")
        match, error = find_unique(content, old_string)
        if match is None:
            return ToolOutput(success=False, error=f"Edit {index}: {error}")
        needle_indent, file_indent = indents_for(content, old_string, match)
        spans.append((match.start, match.end, reindent(replacement, needle_indent, file_indent), index))

    # Check for overlapping edit spans
    sorted_by_start = sorted(spans, key=lambda s: s[0])
    for i in range(len(sorted_by_start) - 1):
        if sorted_by_start[i][1] > sorted_by_start[i + 1][0]:
            return ToolOutput(
                success=False,
                error=f"Edits {sorted_by_start[i][3]} and {sorted_by_start[i+1][3]} overlap in the target file.",
            )

    # Apply replacements from end to start so character offsets remain exact
    new_content = content
    for start, end, replacement, _ in sorted(spans, key=lambda s: s[0], reverse=True):
        new_content = new_content[:start] + replacement + new_content[end:]

    return new_content, content, len(edits)


def _parse_search_replace_blocks(text: str) -> list[tuple[str, str]]:
    """Stateful parser for <<<<<<< SEARCH ... ======= ... >>>>>>> REPLACE blocks.

    Unlike naive regexes, properly handles ======= divider comments inside search blocks.
    """
    blocks: list[tuple[str, str]] = []
    lines = text.splitlines(keepends=True)
    state = "OUTSIDE"
    search_lines: list[str] = []
    replace_lines: list[str] = []

    for line in lines:
        stripped = line.rstrip("\r\n")
        if state == "OUTSIDE":
            if stripped == "<<<<<<< SEARCH":
                state = "SEARCH"
                search_lines = []
                replace_lines = []
        elif state == "SEARCH":
            if stripped == "=======":
                state = "REPLACE"
            else:
                search_lines.append(line)
        elif state == "REPLACE":
            if stripped == ">>>>>>> REPLACE":
                blocks.append(("".join(search_lines), "".join(replace_lines)))
                state = "OUTSIDE"
            else:
                replace_lines.append(line)
    return blocks


def _plan_blocks_or_legacy(content: str, old_string: str | None, new_string: str) -> tuple[str, str, int] | ToolOutput:
    blocks = _parse_search_replace_blocks(new_string)
    if blocks:
        new_content = content
        for search_val, replace_val in blocks:
            match, error = find_unique(new_content, search_val)
            if match is None:
                return ToolOutput(success=False, error=f"SEARCH block: {error}")
            needle_indent, file_indent = indents_for(new_content, search_val, match)
            shifted = reindent(replace_val, needle_indent, file_indent)
            new_content = new_content[: match.start] + shifted + new_content[match.end :]
        return new_content, content, len(blocks)

    if old_string is None:
        return ToolOutput(
            success=False,
            error="Either old_string must be provided, or new_string must contain SEARCH/REPLACE blocks.",
        )
    match, error = find_unique(content, old_string)
    if match is None:
        return ToolOutput(success=False, error=error)
    needle_indent, file_indent = indents_for(content, old_string, match)
    shifted = reindent(new_string, needle_indent, file_indent)
    return content[: match.start] + shifted + content[match.end :], content, 1


def _write(path: Path, old_content: str, new_content: str, blocks_applied: int) -> ToolOutput:
    try:
        current_content = path.read_text(encoding="utf-8")
    except OSError as exc:
        return ToolOutput(success=False, error=str(exc))
    if current_content != old_content:
        return ToolOutput(
            success=False,
            error=(
                f"File `{path.name}` was modified concurrently on disk while awaiting approval. "
                "Edit aborted to prevent data loss."
            ),
        )
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
