"""GlobTool — find files by name pattern, newest first.

A name-matching counterpart to SearchFilesTool (which matches content).
"Find every **/*Test*.ts" is a single call instead of a bash `find` that burns
tokens on noise. Results are sorted by modification time, newest first, so the
most recently touched files surface to the model.

See docs/CODING_STYLE.md Section 16.1.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from tools._path import PRUNED_DIRS, SENSITIVE_DIR_NAMES, resolve_path
from tools.base import Tool
from tools.models import ToolInput, ToolOutput

_MAX_RESULTS = 200


class GlobTool(Tool):
    """Find files matching a glob pattern, sorted by modification time."""

    name = "glob"
    description = (
        "Find files by name using a glob pattern (e.g. '**/*.py', 'src/**/*Test*.ts'). "
        "Returns matching file paths sorted by modification time, newest first. "
        "Use this for name-based lookups; use search_files to match file contents."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Glob pattern, e.g. '**/*.py'"},
            "path": {
                "type": "string",
                "description": "Root directory to search from (default '.')",
                "default": ".",
            },
            "head_limit": {
                "type": "integer",
                "description": f"Max results to return (default and cap {_MAX_RESULTS})",
            },
            "workspace": {"type": "string", "description": "Workspace root (optional)"},
        },
        "required": ["pattern"],
    }

    def format_output(self, data: dict[str, Any]) -> str:
        files = data.get("files", [])
        if not files:
            return "No files matched."
        suffix = "\n… (more results truncated)" if data.get("truncated") else ""
        return "\n".join(files) + suffix

    async def run(self, input: ToolInput) -> ToolOutput:
        pattern = input.params.get("pattern")
        if not pattern:
            return ToolOutput(success=False, error="Parameter 'pattern' is required.")

        resolved = resolve_path(input.params.get("path", "."), input.params.get("workspace"))
        if resolved is None:
            return ToolOutput(success=False, error="Path escapes workspace root.")

        limit = _coerce_limit(input.params.get("head_limit"))
        return await asyncio.to_thread(_glob_sync, resolved, pattern, limit)


def _coerce_limit(value: Any) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return _MAX_RESULTS
    return min(limit, _MAX_RESULTS) if limit >= 1 else _MAX_RESULTS


_SKIPPED_DIR_NAMES = PRUNED_DIRS | SENSITIVE_DIR_NAMES


def _is_pruned(path: Path, base: Path) -> bool:
    return any(part in _SKIPPED_DIR_NAMES for part in path.relative_to(base).parts)


def _glob_sync(base: Path, pattern: str, limit: int) -> ToolOutput:
    if not base.is_dir():
        return ToolOutput(success=False, error=f"Not a directory: {base}")

    try:
        candidates = [p for p in base.glob(pattern) if p.is_file() and not _is_pruned(p, base)]
    except (ValueError, OSError) as exc:
        return ToolOutput(success=False, error=f"Invalid glob pattern: {exc}")

    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    truncated = len(candidates) > limit
    files = [str(p) for p in candidates[:limit]]
    return ToolOutput(
        success=True,
        data={"files": files, "count": len(files), "truncated": truncated},
    )
