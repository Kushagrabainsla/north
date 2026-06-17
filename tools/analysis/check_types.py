"""Type checking via language-specific tools, run in project mode.

Each checker resolves the project root (shared find_project_root helper) so
config files (tsconfig.json, mypy config, go.mod) and imports resolve the way
they do in CI:

- Python: the project's .venv interpreter (fallback sys.executable) -m mypy,
  cwd = project root, file referenced relative to the root.
- TypeScript: project mode ``tsc --noEmit -p <tsconfig>`` with a locally
  resolved tsc (node_modules/.bin or PATH, else ``npx --no-install`` - never
  auto-downloading from the npm registry).
- Go: ``go vet`` on the file's package, run from the go.mod module root.

Unsupported file types return a neutral "skipped" success so a coding agent
does not halt on files no checker covers.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from tools._path import find_project_root, resolve_path
from tools.base import Tool
from tools.models import ToolInput, ToolOutput

_TIMEOUT = 60


class CheckTypesTool(Tool):
    """Run language-specific type checkers on a file."""

    name = "check_types"
    description = (
        "Run language-specific type checking on a file, using the project's own "
        "configuration (mypy from the project root, tsc via tsconfig.json, go vet "
        "on the file's package from the go.mod root). "
        "Returns type errors with line numbers. Files in unsupported languages "
        "return a successful 'skipped' result, not an error."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File path to check",
            },
            "workspace": {"type": "string", "description": "Workspace root (optional)"},
        },
        "required": ["path"],
    }

    def format_output(self, data: dict[str, Any]) -> str:
        if data.get("skipped"):
            return str(data.get("reason", "Type checking skipped."))
        errors = len(data.get("errors", []))
        warnings = len(data.get("warnings", []))
        return f"Type check: {errors} errors, {warnings} warnings."

    async def run(self, input: ToolInput) -> ToolOutput:
        path_str = input.params.get("path")
        if not path_str:
            return ToolOutput(success=False, error="Parameter 'path' is required.")

        resolved = resolve_path(path_str, input.params.get("workspace"))
        if resolved is None:
            return ToolOutput(success=False, error="Path escapes workspace root.")

        if not resolved.exists():
            return ToolOutput(success=False, error=f"File not found: {resolved}")
        if not resolved.is_file():
            return ToolOutput(success=False, error=f"Not a file: {resolved}")

        return await asyncio.to_thread(_check_types_sync, resolved)


def _skipped(path: Path, reason: str) -> ToolOutput:
    """Neutral non-failure for files no checker covers - the agent should move on."""
    return ToolOutput(success=True, data={"file": str(path), "skipped": True, "reason": reason})


def _check_types_sync(path: Path) -> ToolOutput:
    suffix = path.suffix
    if suffix == ".py":
        return _check_python(path)
    if suffix in (".ts", ".tsx"):
        return _check_typescript(path)
    if suffix == ".go":
        return _check_go(path)
    return _skipped(path, f"Type checking not supported for {suffix!r} files; skipping.")


def _run_checker(cmd: list[str], cwd: Path) -> tuple[str, str | None]:
    """Run a checker subprocess. Returns (stdout+stderr, error-or-None)."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=_TIMEOUT, cwd=str(cwd))
    except subprocess.TimeoutExpired:
        return "", f"{cmd[0]} timed out after {_TIMEOUT}s."
    except FileNotFoundError:
        return "", f"Executable not found: {cmd[0]}"
    except Exception as exc:
        return "", f"Error running {cmd[0]}: {exc}"
    return result.stdout + ("\n" + result.stderr if result.stderr else ""), None


def _collect(path: Path, output: str, lang: str, error_marker: str, warning_markers: tuple[str, ...]) -> ToolOutput:
    errors: list[str] = []
    warnings: list[str] = []
    parsed_errors: list[dict] = []
    for line in output.splitlines():
        if error_marker in line:
            errors.append(line)
        elif any(marker in line for marker in warning_markers):
            warnings.append(line)
        else:
            continue
        if parsed := _parse_error_line(line, lang):
            parsed_errors.append(parsed)

    return ToolOutput(
        success=len(errors) == 0,
        data={
            "file": str(path),
            "errors": errors,
            "warnings": warnings,
            "parsed_errors": parsed_errors,
            "raw_output": output,
        },
    )


def _check_python(path: Path) -> ToolOutput:
    root = find_project_root(path)
    venv_python = root / ".venv" / "bin" / "python"
    interpreter = str(venv_python) if venv_python.exists() else sys.executable
    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = path

    output, error = _run_checker([interpreter, "-m", "mypy", "--no-error-summary", str(rel)], cwd=root)
    if error:
        return ToolOutput(success=False, error=error)
    if "No module named mypy" in output:
        return _skipped(path, "mypy is not installed in this project; skipping type check.")
    return _collect(path, output, "python", "error:", ("warning:", "note:"))


def _resolve_tsc(root: Path) -> list[str] | None:
    """Locate a local tsc; never auto-download from the npm registry."""
    local = root / "node_modules" / ".bin" / "tsc"
    if local.exists():
        return [str(local)]
    if shutil.which("tsc"):
        return ["tsc"]
    if shutil.which("npx"):
        return ["npx", "--no-install", "tsc"]
    return None


def _check_typescript(path: Path) -> ToolOutput:
    root = find_project_root(path, markers=("tsconfig.json", "package.json", ".git"))
    tsc = _resolve_tsc(root)
    if tsc is None:
        return _skipped(path, "No local TypeScript compiler found (tsc/npx); skipping type check.")

    tsconfig = _find_upward(path, "tsconfig.json", stop=root)
    # Project mode (tsc loads tsconfig.json - paths/jsx/lib apply) when a config
    # exists; otherwise fall back to checking the single file directly.
    cmd = [*tsc, "--noEmit", "-p", str(tsconfig)] if tsconfig is not None else [*tsc, "--noEmit", str(path)]

    output, error = _run_checker(cmd, cwd=root)
    if error:
        return ToolOutput(success=False, error=error)
    return _collect(path, output, "typescript", "error TS", ("warning",))


def _check_go(path: Path) -> ToolOutput:
    go_mod = _find_upward(path, "go.mod")
    if go_mod is None:
        return _skipped(path, "No go.mod found above this file; skipping go vet.")
    module_root = go_mod.parent
    if not shutil.which("go"):
        return _skipped(path, "Go toolchain not found; skipping go vet.")

    pkg_dir = path.parent.relative_to(module_root)
    pkg = "./" + str(pkg_dir) if str(pkg_dir) != "." else "./."
    output, error = _run_checker(["go", "vet", pkg], cwd=module_root)
    if error:
        return ToolOutput(success=False, error=error)

    errors: list[str] = []
    parsed_errors: list[dict] = []
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        errors.append(line)
        if parsed := _parse_error_line(line, "go"):
            parsed_errors.append(parsed)

    return ToolOutput(
        success=len(errors) == 0,
        data={
            "file": str(path),
            "errors": errors,
            "warnings": [],
            "parsed_errors": parsed_errors,
            "raw_output": output,
        },
    )


def _find_upward(path: Path, filename: str, stop: Path | None = None) -> Path | None:
    """Return the nearest *filename* in path's parent chain (inclusive of *stop*)."""
    start = path if path.is_dir() else path.parent
    for directory in (start, *start.parents):
        candidate = directory / filename
        if candidate.exists():
            return candidate
        if stop is not None and directory == stop:
            return None
    return None


def _parse_error_line(line: str, lang: str) -> dict | None:
    line = line.strip()
    if not line:
        return None

    if lang == "python":
        m = re.match(r"^([^:]+):(\d+):(?:(\d+):)?\s*(error|warning|note):\s*(.*)$", line)
        if m:
            return {
                "file": m.group(1),
                "line": int(m.group(2)),
                "column": int(m.group(3)) if m.group(3) else None,
                "severity": m.group(4),
                "message": m.group(5),
            }
    elif lang == "typescript":
        m1 = re.match(r"^([^\(\s]+)\((\d+),(\d+)\):\s*(error|warning|note)?\s*(?:TS\d+)?:\s*(.*)$", line, re.IGNORECASE)
        if m1:
            return {
                "file": m1.group(1),
                "line": int(m1.group(2)),
                "column": int(m1.group(3)),
                "severity": m1.group(4) or "error",
                "message": m1.group(5),
            }
        m2 = re.match(r"^([^:\s]+):(\d+):(\d+)\s+-\s*(error|warning|note)?\s*(?:TS\d+)?:\s*(.*)$", line, re.IGNORECASE)
        if m2:
            return {
                "file": m2.group(1),
                "line": int(m2.group(2)),
                "column": int(m2.group(3)),
                "severity": m2.group(4) or "error",
                "message": m2.group(5),
            }
    elif lang == "go":
        m = re.match(r"^([^:]+):(\d+):(?:(\d+):)?\s*(.*)$", line)
        if m:
            return {
                "file": m.group(1),
                "line": int(m.group(2)),
                "column": int(m.group(3)) if m.group(3) else None,
                "severity": "error",
                "message": m.group(4),
            }
    return None
