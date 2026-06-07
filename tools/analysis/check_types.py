"""Type checking via language-specific tools."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from tools._path import resolve_path
from tools.base import Tool
from tools.models import ToolInput, ToolOutput


class CheckTypesTool(Tool):
    """Run language-specific type checkers on a file."""

    name = "check_types"
    description = (
        "Run language-specific type checking on a file. "
        "Supports Python (mypy), TypeScript (tsc), and Go (go vet). "
        "Returns type errors with line numbers."
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


async def _run_command(cmd: list[str], cwd: Path | None = None) -> tuple[int, str, str]:
    """Run a shell command and return (returncode, stdout, stderr)."""
    import subprocess

    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "Command timed out after 30s."
    except Exception as exc:
        return -1, "", str(exc)


def _check_types_sync(path: Path) -> ToolOutput:
    suffix = path.suffix

    if suffix == ".py":
        return _check_python(path)
    elif suffix in (".ts", ".tsx"):
        return _check_typescript(path)
    elif suffix == ".go":
        return _check_go(path)
    else:
        return ToolOutput(
            success=False,
            error=f"check_types does not support .{suffix} files. Supported: .py, .ts, .tsx, .go",
        )


def _check_python(path: Path) -> ToolOutput:
    import subprocess

    try:
        result = subprocess.run(
            [".venv/bin/python", "-m", "mypy", "--no-error-summary", str(path)],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=path.parent,
        )
    except subprocess.TimeoutExpired:
        return ToolOutput(success=False, error="mypy timed out.")
    except FileNotFoundError:
        return ToolOutput(success=False, error="mypy not found in .venv/bin/python.")
    except Exception as exc:
        return ToolOutput(success=False, error=f"Error running mypy: {exc}")

    errors = []
    warnings = []
    for line in result.stdout.splitlines():
        if "error:" in line:
            errors.append(line)
        elif "warning:" in line or "note:" in line:
            warnings.append(line)

    return ToolOutput(
        success=len(errors) == 0,
        data={
            "file": str(path),
            "errors": errors,
            "warnings": warnings,
            "raw_output": result.stdout,
        },
    )


def _check_typescript(path: Path) -> ToolOutput:
    import subprocess

    try:
        result = subprocess.run(
            ["npx", "tsc", "--noEmit", str(path)],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=path.parent,
        )
    except subprocess.TimeoutExpired:
        return ToolOutput(success=False, error="tsc timed out.")
    except FileNotFoundError:
        return ToolOutput(success=False, error="npx/tsc not found.")
    except Exception as exc:
        return ToolOutput(success=False, error=f"Error running tsc: {exc}")

    errors = []
    warnings = []
    for line in result.stdout.splitlines():
        if "error TS" in line:
            errors.append(line)
        elif "warning" in line.lower():
            warnings.append(line)

    return ToolOutput(
        success=len(errors) == 0,
        data={
            "file": str(path),
            "errors": errors,
            "warnings": warnings,
            "raw_output": result.stdout,
        },
    )


def _check_go(path: Path) -> ToolOutput:
    import subprocess

    try:
        result = subprocess.run(
            ["go", "vet", "./..."],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=path.parent,
        )
    except subprocess.TimeoutExpired:
        return ToolOutput(success=False, error="go vet timed out.")
    except FileNotFoundError:
        return ToolOutput(success=False, error="go vet not found.")
    except Exception as exc:
        return ToolOutput(success=False, error=f"Error running go vet: {exc}")

    errors = []
    warnings = []
    for line in result.stdout.splitlines():
        if line.strip():
            errors.append(line)

    return ToolOutput(
        success=len(errors) == 0,
        data={
            "file": str(path),
            "errors": errors,
            "warnings": warnings,
            "raw_output": result.stdout,
        },
    )
