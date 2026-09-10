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
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from approval.policy import Action, ActionKind
from tools._path import resolve_path, scope_refusal
from tools._read_tracker import record_read, was_read
from tools.base import ApprovalGatedTool
from tools.models import ToolInput, ToolOutput
from tools.specialized._approval import gate_action
from tools.specialized._edit_match import (
    detect_line_ending,
    find_unique,
    indents_for,
    normalize_to_lf,
    reindent,
    restore_line_ending,
    split_bom,
)

if TYPE_CHECKING:
    from approval.base import Notifier
    from approval.policy import ApprovalPolicy
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
        policy: ApprovalPolicy | None = None,
        notifier: Notifier | None = None,
    ) -> None:
        super().__init__(approval_store, stream_manager, approval_timeout_seconds, policy, notifier)

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

        # Server-owned edit-scope check, before the read-precondition and any
        # mutation. The scope arrives on input.edit_scope (never params), so the
        # model cannot forge it. A None scope preserves prior behavior.
        if (refusal := scope_refusal(input.edit_scope, resolved)) is not None:
            return ToolOutput(success=False, error=refusal, failure_kind="refused")

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

        if plan.new_content == plan.old_content:
            return ToolOutput(success=True, data={"path": str(resolved), "blocks_applied": 0, "unchanged": True})

        # An instance with no approval store is the plain, auto-discovered file
        # writer the coder uses; app.py replaces it with a gated one at startup.
        # Whether this tool is wired for approval is its own business - what the
        # gate then decides is not.
        if self._approval_store is not None:
            refused = await self._gate(
                input.params.get("task_id"),
                resolved,
                input.params.get("workspace"),
                plan.old_content,
                plan.new_content,
            )
            if refused is not None:
                return refused

        written = await asyncio.to_thread(_write, resolved, plan)
        if written.success:
            record_read(input.params.get("task_id"), str(resolved))
        return written

    async def _gate(
        self, task_id: str | None, path: Path, workspace: str | None, old: str, new: str
    ) -> ToolOutput | None:
        """``None`` when the edit may be written; otherwise what to return instead."""
        diff = _unified_diff(path, old, new)
        return await gate_action(
            Action(
                agent="patch_file",
                kind=ActionKind.FILE_EDIT,
                summary=f"edit {path}",
                path=path,
                workspace=workspace or "",
            ),
            policy=self._policy,
            approval_store=self._approval_store,
            title="File Edit - Approval Required",
            message=f"Apply this change to `{path}`?\n```diff\n{diff}\n```",
            options=("Apply", "Cancel"),
            task_id=task_id,
            stream_manager=self._stream_manager,
            notifier=self._notifier,
            timeout=self._approval_timeout_seconds,
            declined="Edit cancelled by user.",
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


@dataclass(frozen=True, slots=True)
class EditPlan:
    """A computed, not-yet-written change, in LF space plus how to restore bytes.

    Matching and diffing happen on LF-normalized, BOM-stripped text so a model's
    ``old_string`` (always ``\\n``, never an invisible BOM) matches a CRLF or
    BOM file. The original bytes are rebuilt on write from ``bom`` + the file's
    own line ending, and ``raw_original`` is what the concurrent-modification
    check compares against so an on-disk change is still caught exactly.
    """

    old_content: str  # LF-normalized, BOM-stripped
    new_content: str  # LF-normalized, BOM-stripped
    blocks_applied: int
    bom: str
    ending: str
    raw_original: str


def _plan(path: Path, edits: Any, old_string: str | None, new_string: str | None) -> EditPlan | ToolOutput:
    """Compute the would-be new file content without writing it.

    Returns an :class:`EditPlan` or a ToolOutput on error. All matching is done
    in LF-normalized, BOM-stripped space; the plan carries what is needed to
    restore the file's original line ending and BOM on write.
    """
    if not path.exists() or not path.is_file():
        return ToolOutput(success=False, error=f"File not found: {path}", failure_kind="not_found")
    try:
        # Read raw bytes, not read_text: universal-newline mode would translate
        # CRLF to LF on read and the original ending would be lost before we can
        # record it.
        raw = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        return ToolOutput(success=False, error=f"Binary file cannot be patched: {path}")

    bom, body = split_bom(raw)
    ending = detect_line_ending(body)
    content = normalize_to_lf(body)

    if edits is not None:
        result = _plan_edits(content, edits)
    else:
        result = _plan_blocks_or_legacy(content, old_string, new_string or "")
    if isinstance(result, ToolOutput):
        return result
    new_content, blocks_applied = result
    return EditPlan(
        old_content=content,
        new_content=new_content,
        blocks_applied=blocks_applied,
        bom=bom,
        ending=ending,
        raw_original=raw,
    )


def _apply_spans(content: str, spans: list[tuple[int, int, str, str]]) -> tuple[str, str] | ToolOutput:
    """Overlap-check *spans* against *content* and apply them end-to-start.

    Each span is ``(start, end, replacement, label)`` where offsets are into the
    original *content* - never a buffer mutated by an earlier edit - so two edits
    that touch the same region are caught up front rather than one silently
    failing to match. Returns ``(new_content, "")`` or a ToolOutput on overlap.
    """
    by_start = sorted(spans, key=lambda s: s[0])
    for i in range(len(by_start) - 1):
        if by_start[i][1] > by_start[i + 1][0]:
            return ToolOutput(
                success=False,
                error=f"Edits {by_start[i][3]} and {by_start[i + 1][3]} overlap in the target file.",
            )
    new_content = content
    for start, end, replacement, _label in sorted(spans, key=lambda s: s[0], reverse=True):
        new_content = new_content[:start] + replacement + new_content[end:]
    return new_content, ""


def _plan_edits(content: str, edits: Any) -> tuple[str, int] | ToolOutput:
    if not isinstance(edits, list) or not edits:
        return ToolOutput(success=False, error="'edits' must be a non-empty list.")
    spans: list[tuple[int, int, str, str]] = []
    for index, edit in enumerate(edits):
        if not isinstance(edit, dict):
            return ToolOutput(success=False, error=f"Edit {index} is not an object.")
        old_string = edit.get("old_string")
        replacement = edit.get("new_string")
        if old_string is None or replacement is None:
            return ToolOutput(success=False, error=f"Edit {index} needs both 'old_string' and 'new_string'.")
        old_string = normalize_to_lf(old_string)
        replacement = normalize_to_lf(replacement)
        match, error = find_unique(content, old_string)
        if match is None:
            return ToolOutput(success=False, error=f"Edit {index}: {error}")
        needle_indent, file_indent = indents_for(content, old_string, match)
        spans.append((match.start, match.end, reindent(replacement, needle_indent, file_indent), f"#{index}"))

    applied = _apply_spans(content, spans)
    if isinstance(applied, ToolOutput):
        return applied
    return applied[0], len(edits)


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


def _plan_blocks_or_legacy(content: str, old_string: str | None, new_string: str) -> tuple[str, int] | ToolOutput:
    blocks = _parse_search_replace_blocks(new_string)
    if blocks:
        # Match every block against the *original* content and overlap-check once,
        # the same discipline as the edits[] path, so overlapping blocks are
        # reported up front rather than mis-applying against a mutated buffer.
        spans: list[tuple[int, int, str, str]] = []
        for i, (search_val, replace_val) in enumerate(blocks):
            match, error = find_unique(content, search_val)
            if match is None:
                return ToolOutput(success=False, error=f"SEARCH block {i}: {error}")
            needle_indent, file_indent = indents_for(content, search_val, match)
            spans.append((match.start, match.end, reindent(replace_val, needle_indent, file_indent), f"block {i}"))
        applied = _apply_spans(content, spans)
        if isinstance(applied, ToolOutput):
            return applied
        return applied[0], len(blocks)

    if old_string is None:
        return ToolOutput(
            success=False,
            error="Either old_string must be provided, or new_string must contain SEARCH/REPLACE blocks.",
        )
    old_string = normalize_to_lf(old_string)
    match, error = find_unique(content, old_string)
    if match is None:
        return ToolOutput(success=False, error=error)
    needle_indent, file_indent = indents_for(content, old_string, match)
    shifted = reindent(new_string, needle_indent, file_indent)
    return content[: match.start] + shifted + content[match.end :], 1


def _write(path: Path, plan: EditPlan) -> ToolOutput:
    try:
        # Raw bytes, to compare and write without newline translation.
        current_raw = path.read_bytes().decode("utf-8")
    except OSError as exc:
        return ToolOutput(success=False, error=str(exc))
    except UnicodeDecodeError as exc:
        return ToolOutput(success=False, error=str(exc))
    if current_raw != plan.raw_original:
        return ToolOutput(
            success=False,
            error=(
                f"File `{path.name}` was modified concurrently on disk while awaiting approval. "
                "Edit aborted to prevent data loss."
            ),
        )
    final = plan.bom + restore_line_ending(plan.new_content, plan.ending)
    try:
        path.write_bytes(final.encode("utf-8"))
    except OSError as exc:
        return ToolOutput(success=False, error=str(exc))
    return ToolOutput(
        success=True,
        data={
            "path": str(path),
            "bytes_before": len(plan.raw_original.encode("utf-8")),
            "bytes_after": len(final.encode("utf-8")),
            "blocks_applied": plan.blocks_applied,
        },
    )
