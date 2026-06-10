"""SearchFilesTool — grep-style content search across workspace files.

Supports the modes a coding agent actually needs: matching lines with context,
a bare list of matching files, or per-file match counts — filtered by file glob
or language type. Pure-Python (stdlib `re`); no external ripgrep binary required.

See docs/CODING_STYLE.md Section 16.1.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from tools._path import resolve_path
from tools.base import Tool
from tools.models import ToolInput, ToolOutput

_MAX_MATCHES = 200
# Directories never worth walking for a coding task.
_PRUNED_DIRS: frozenset[str] = frozenset(
    {".git", "node_modules", "__pycache__", ".venv", "venv", ".ruff_cache", ".pytest_cache", "build", "dist"}
)
# Language type → file globs, mirroring ripgrep's --type shorthands.
_TYPE_GLOBS: dict[str, tuple[str, ...]] = {
    "py": ("*.py",),
    "ts": ("*.ts", "*.tsx"),
    "js": ("*.js", "*.jsx", "*.mjs"),
    "go": ("*.go",),
    "rust": ("*.rs",),
    "java": ("*.java",),
    "md": ("*.md",),
    "yaml": ("*.yaml", "*.yml"),
    "json": ("*.json",),
}
_OUTPUT_MODES: frozenset[str] = frozenset({"content", "files_with_matches", "count"})


class SearchFilesTool(Tool):
    """Search files recursively for a regex pattern, with grep-style output modes."""

    name = "search_files"
    description = (
        "Search file contents recursively for a regex pattern. "
        "output_mode 'content' returns matching lines (optionally with context), "
        "'files_with_matches' returns just the file paths, 'count' returns per-file "
        "match counts. Filter with file_glob or file_type (py, ts, js, go, ...)."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Regex pattern to search for"},
            "path": {"type": "string", "description": "Root path to search from (default '.')", "default": "."},
            "file_glob": {"type": "string", "description": "File glob filter (default '*')", "default": "*"},
            "file_type": {
                "type": "string",
                "description": "Language filter shorthand: py, ts, js, go, rust, java, md, yaml, json",
            },
            "output_mode": {
                "type": "string",
                "enum": sorted(_OUTPUT_MODES),
                "description": "content (default), files_with_matches, or count",
                "default": "content",
            },
            "context": {
                "type": "integer",
                "description": "Lines of context to show before and after each match (content mode)",
            },
            "head_limit": {
                "type": "integer",
                "description": f"Max matches/files to return (default and cap {_MAX_MATCHES})",
            },
            "workspace": {"type": "string", "description": "Workspace root (optional)"},
        },
        "required": ["pattern"],
    }

    def format_output(self, data: dict[str, Any]) -> str:
        mode = data.get("output_mode", "content")
        if mode == "files_with_matches":
            files = data.get("files", [])
            return "\n".join(files) if files else "No files matched."
        if mode == "count":
            counts = data.get("counts", [])
            return "\n".join(f"{c['file']}: {c['count']}" for c in counts) if counts else "No matches found."
        matches = data.get("matches", [])
        return "\n".join(f"{m['file']}:{m['line']}: {m['text']}" for m in matches) if matches else "No matches found."

    async def run(self, input: ToolInput) -> ToolOutput:
        pattern = input.params.get("pattern")
        if not pattern:
            return ToolOutput(success=False, error="Parameter 'pattern' is required.")

        resolved = resolve_path(input.params.get("path", "."), input.params.get("workspace"))
        if resolved is None:
            return ToolOutput(success=False, error="Path escapes workspace root.")

        mode = input.params.get("output_mode", "content")
        if mode not in _OUTPUT_MODES:
            return ToolOutput(success=False, error=f"output_mode must be one of {sorted(_OUTPUT_MODES)}.")

        options = _SearchOptions(
            globs=_resolve_globs(input.params.get("file_glob", "*"), input.params.get("file_type")),
            mode=mode,
            context=_coerce_int(input.params.get("context"), default=0, minimum=0),
            limit=_coerce_int(input.params.get("head_limit"), default=_MAX_MATCHES, minimum=1, cap=_MAX_MATCHES),
        )
        return await asyncio.to_thread(_search_sync, resolved, pattern, options)


class _SearchOptions:
    __slots__ = ("globs", "mode", "context", "limit")

    def __init__(self, globs: tuple[str, ...], mode: str, context: int, limit: int) -> None:
        self.globs = globs
        self.mode = mode
        self.context = context
        self.limit = limit


def _coerce_int(value: Any, *, default: int, minimum: int, cap: int | None = None) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    if n < minimum:
        return default
    return min(n, cap) if cap is not None else n


def _resolve_globs(file_glob: str, file_type: str | None) -> tuple[str, ...]:
    if file_type:
        return _TYPE_GLOBS.get(file_type.lower(), (file_glob or "*",))
    return (file_glob or "*",)


def _iter_files(base: Path, globs: tuple[str, ...]):
    if base.is_file():
        yield base
        return
    seen: set[Path] = set()
    for glob in globs:
        for file in base.rglob(glob):
            if file in seen or not file.is_file():
                continue
            if any(part in _PRUNED_DIRS for part in file.relative_to(base).parts):
                continue
            seen.add(file)
            yield file


def _search_sync(base: Path, pattern: str, options: _SearchOptions) -> ToolOutput:
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        return ToolOutput(success=False, error=f"Invalid regex: {exc}")

    matches: list[dict] = []
    files: list[str] = []
    counts: list[dict] = []

    for file in _iter_files(base, options.globs):
        try:
            lines = file.read_text(encoding="utf-8").splitlines()
        except (UnicodeDecodeError, OSError):
            continue

        hit_lines = [i for i, line in enumerate(lines) if regex.search(line)]
        if not hit_lines:
            continue

        if options.mode == "files_with_matches":
            files.append(str(file))
            if len(files) >= options.limit:
                break
            continue
        if options.mode == "count":
            counts.append({"file": str(file), "count": len(hit_lines)})
            if len(counts) >= options.limit:
                break
            continue

        include: set[int] = set()
        for i in hit_lines:
            include.update(range(max(0, i - options.context), min(len(lines), i + options.context + 1)))
        for j in sorted(include):
            matches.append({"file": str(file), "line": j + 1, "text": lines[j]})
            if len(matches) >= options.limit:
                break
        if len(matches) >= options.limit:
            break

    data: dict[str, Any] = {"output_mode": options.mode}
    if options.mode == "files_with_matches":
        data["files"] = files
    elif options.mode == "count":
        data["counts"] = counts
    else:
        data["matches"] = matches
    return ToolOutput(success=True, data=data)
