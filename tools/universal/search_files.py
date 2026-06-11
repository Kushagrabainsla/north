"""SearchFilesTool — grep-style content search across workspace files.

Supports the modes a coding agent actually needs: matching lines with context,
a bare list of matching files, or per-file match counts — filtered by file glob
or language type. Uses the ripgrep binary when one is installed (fast,
.gitignore-aware, skips binary files); falls back to a pure-Python stdlib
search so the tool always works.

See docs/CODING_STYLE.md Section 16.1.
"""

from __future__ import annotations

import asyncio
import functools
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from tools._path import PRUNED_DIRS, resolve_path
from tools.base import Tool
from tools.models import ToolInput, ToolOutput

_MAX_MATCHES = 200
_RG_TIMEOUT_SECONDS = 30
# Common install locations checked when `rg` is not on PATH (launchd services
# often run with a minimal PATH that misses Homebrew/cargo).
_RG_CANDIDATE_PATHS: tuple[str, ...] = (
    "/opt/homebrew/bin/rg",
    "/usr/local/bin/rg",
    "~/.cargo/bin/rg",
    "/usr/bin/rg",
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


@functools.lru_cache(maxsize=1)
def _rg_binary() -> str | None:
    """Locate ripgrep: NORTH_RIPGREP override → bundled wheel → PATH → known locations.

    The `ripgrep` PyPI dependency (platform-marked in pyproject.toml) installs
    an `rg` binary next to the interpreter, so most installs find it there
    without the user installing anything system-wide.
    """
    override = os.environ.get("NORTH_RIPGREP", "").strip()
    if override:
        p = Path(override).expanduser()
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
    bundled = Path(sys.executable).parent / ("rg.exe" if sys.platform == "win32" else "rg")
    if bundled.is_file() and os.access(bundled, os.X_OK):
        return str(bundled)
    found = shutil.which("rg")
    if found:
        return found
    for candidate in _RG_CANDIDATE_PATHS:
        p = Path(candidate).expanduser()
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
    return None


class SearchFilesTool(Tool):
    """Search files recursively for a regex pattern, with grep-style output modes."""

    name = "search_files"
    description = (
        "Search file contents recursively for a regex pattern. "
        "output_mode 'content' returns matching lines (optionally with context), "
        "'files_with_matches' returns just the file paths, 'count' returns per-file "
        "match counts. Filter with file_glob or file_type (py, ts, js, go, ...). "
        "Uses ripgrep when installed: fast and .gitignore-aware."
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
            "case_insensitive": {
                "type": "boolean",
                "description": "Match case-insensitively (default false)",
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
            case_insensitive=bool(input.params.get("case_insensitive", False)),
        )

        rg = _rg_binary()
        if rg is not None:
            result = await asyncio.to_thread(_search_rg, rg, resolved, pattern, options)
            if result is not None:
                return result
            # rg failed structurally (spawn error, unexpected exit) — fall back.
        return await asyncio.to_thread(_search_sync, resolved, pattern, options)


class _SearchOptions:
    __slots__ = ("globs", "mode", "context", "limit", "case_insensitive")

    def __init__(
        self, globs: tuple[str, ...], mode: str, context: int, limit: int, case_insensitive: bool = False
    ) -> None:
        self.globs = globs
        self.mode = mode
        self.context = context
        self.limit = limit
        self.case_insensitive = case_insensitive


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


# ---------------------------------------------------------------------------
# ripgrep engine
# ---------------------------------------------------------------------------


def _build_rg_command(rg: str, base: Path, pattern: str, options: _SearchOptions) -> list[str]:
    # --hidden searches dotfiles (.github/, .env...) which agents legitimately
    # need; the pruned-dir globs keep .git and dependency dirs out everywhere,
    # including outside git repos where .gitignore doesn't apply.
    cmd = [rg, "--sort", "path", "--hidden", "--no-messages"]
    for d in sorted(PRUNED_DIRS):
        cmd += ["-g", f"!**/{d}/**"]
    for g in options.globs:
        if g and g != "*":
            cmd += ["-g", g]
    if options.case_insensitive:
        cmd.append("-i")
    if options.mode == "files_with_matches":
        cmd.append("-l")
    elif options.mode == "count":
        cmd.append("--count")
    else:
        cmd += ["--json", "--max-count", str(options.limit)]
        if options.context > 0:
            cmd += ["-C", str(options.context)]
    cmd += ["-e", pattern, str(base)]
    return cmd


def _parse_rg_content(stdout: str, limit: int) -> list[dict]:
    """Parse `rg --json` event lines into {file, line, text} match dicts."""
    matches: list[dict] = []
    for raw in stdout.splitlines():
        if len(matches) >= limit:
            break
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if event.get("type") not in ("match", "context"):
            continue
        data = event.get("data", {})
        file = (data.get("path") or {}).get("text")
        text = (data.get("lines") or {}).get("text")
        line = data.get("line_number")
        if file is None or text is None or line is None:
            continue
        matches.append({"file": file, "line": line, "text": text.rstrip("\n")})
    return matches


def _search_rg(rg: str, base: Path, pattern: str, options: _SearchOptions) -> ToolOutput | None:
    """Search with ripgrep. Returns None when the Python fallback should run instead."""
    cmd = _build_rg_command(rg, base, pattern, options)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=_RG_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return ToolOutput(success=False, error=f"Search timed out after {_RG_TIMEOUT_SECONDS}s.")
    except (FileNotFoundError, OSError):
        return None

    # rg exit codes: 0 = matches, 1 = no matches, 2 = error (bad pattern, etc.).
    # Anything else (126/127 from a broken binary, signals) → Python fallback.
    if proc.returncode == 2:
        stderr = proc.stderr.strip()
        if "regex parse error" in stderr or "error parsing" in stderr:
            return ToolOutput(success=False, error=f"Invalid regex: {stderr.splitlines()[0] if stderr else pattern}")
        return None
    if proc.returncode not in (0, 1):
        return None

    data: dict[str, Any] = {"output_mode": options.mode, "engine": "ripgrep"}
    if options.mode == "files_with_matches":
        data["files"] = proc.stdout.splitlines()[: options.limit]
    elif options.mode == "count":
        counts: list[dict] = []
        for line in proc.stdout.splitlines():
            if len(counts) >= options.limit:
                break
            file, sep, count = line.rpartition(":")
            if sep and count.isdigit():
                counts.append({"file": file, "count": int(count)})
        data["counts"] = counts
    else:
        data["matches"] = _parse_rg_content(proc.stdout, options.limit)
    return ToolOutput(success=True, data=data)


# ---------------------------------------------------------------------------
# pure-Python fallback engine
# ---------------------------------------------------------------------------


def _iter_files(base: Path, globs: tuple[str, ...]):
    if base.is_file():
        yield base
        return
    seen: set[Path] = set()
    for glob in globs:
        for file in base.rglob(glob):
            if file in seen or not file.is_file():
                continue
            if any(part in PRUNED_DIRS for part in file.relative_to(base).parts):
                continue
            seen.add(file)
            yield file


def _search_sync(base: Path, pattern: str, options: _SearchOptions) -> ToolOutput:
    try:
        regex = re.compile(pattern, re.IGNORECASE if options.case_insensitive else 0)
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

    data: dict[str, Any] = {"output_mode": options.mode, "engine": "python"}
    if options.mode == "files_with_matches":
        data["files"] = files
    elif options.mode == "count":
        data["counts"] = counts
    else:
        data["matches"] = matches
    return ToolOutput(success=True, data=data)
