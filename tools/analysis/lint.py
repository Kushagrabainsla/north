"""Linting and formatting via the project's own tools, run in project mode.

Mirrors check_types: resolve the project root so config (ruff.toml/pyproject,
.eslintrc, go.mod) applies exactly as in CI, run the language's linter/formatter,
and return structured issues. Pass ``fix=true`` to auto-fix and format in place.

- Python: the project's ruff (``.venv/bin/ruff`` or PATH) - ``ruff check`` plus
  ``ruff format --check`` (``--fix`` + ``format`` when fixing).
- JS/TS: a locally resolved eslint (node_modules/.bin or ``npx --no-install``).
- Go: ``gofmt -l`` (``-w`` when fixing).

Unsupported files return a neutral "skipped" success so an agent never halts.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from tools._path import find_project_root, resolve_path
from tools.base import Tool
from tools.models import ToolInput, ToolOutput

_TIMEOUT = 60


class LintTool(Tool):
    """Run the project's linter/formatter on a file, optionally auto-fixing."""

    name = "lint"
    description = (
        "Run the project's linter/formatter on a file (ruff for Python, eslint for "
        "JS/TS, gofmt for Go), using the project's own config. Reports style/lint "
        "issues with line numbers. Pass fix=true to auto-fix and format in place. "
        "Files in unsupported languages return a successful 'skipped' result."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path to lint"},
            "fix": {"type": "boolean", "description": "Auto-fix and format in place (default false)"},
            "workspace": {"type": "string", "description": "Workspace root (optional)"},
        },
        "required": ["path"],
    }

    def format_output(self, data: dict[str, Any]) -> str:
        if data.get("skipped"):
            return str(data.get("reason", "Linting skipped."))
        issues = len(data.get("issues", []))
        fixed = " (auto-fixed what it could)" if data.get("fixed") else ""
        return f"Lint: {issues} issue(s){fixed}." if issues else f"Lint: clean{fixed}."

    async def run(self, input: ToolInput) -> ToolOutput:
        path_str = input.params.get("path")
        if not path_str:
            return ToolOutput(success=False, error="Parameter 'path' is required.")
        resolved = resolve_path(path_str, input.params.get("workspace"))
        if resolved is None:
            return ToolOutput(success=False, error="Path escapes workspace root.")
        if not resolved.is_file():
            return ToolOutput(success=False, error=f"Not a file: {resolved}")
        fix = bool(input.params.get("fix", False))
        return await asyncio.to_thread(_lint_sync, resolved, fix)


def _skipped(path: Path, reason: str) -> ToolOutput:
    return ToolOutput(success=True, data={"file": str(path), "skipped": True, "reason": reason})


def _run(cmd: list[str], cwd: Path) -> tuple[str, int, str | None]:
    """Run a tool subprocess. Returns (stdout+stderr, returncode, error-or-None)."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=_TIMEOUT, cwd=str(cwd))
    except subprocess.TimeoutExpired:
        return "", 1, f"{cmd[0]} timed out after {_TIMEOUT}s."
    except FileNotFoundError:
        return "", 1, f"Executable not found: {cmd[0]}"
    except Exception as exc:  # noqa: BLE001
        return "", 1, f"Error running {cmd[0]}: {exc}"
    return result.stdout + ("\n" + result.stderr if result.stderr else ""), result.returncode, None


def _resolve_exe(root: Path, name: str) -> str | None:
    local = root / ".venv" / "bin" / name
    if local.exists():
        return str(local)
    node_local = root / "node_modules" / ".bin" / name
    if node_local.exists():
        return str(node_local)
    return name if shutil.which(name) else None


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _lint_sync(path: Path, fix: bool) -> ToolOutput:
    suffix = path.suffix.lower()
    if suffix == ".py":
        return _lint_python(path, fix)
    if suffix in (".ts", ".tsx", ".js", ".jsx"):
        return _lint_eslint(path, fix)
    if suffix == ".go":
        return _lint_go(path, fix)
    return _skipped(path, f"Linting not supported for {suffix!r} files; skipping.")


def _result(path: Path, issues: list[str], fixed: bool, raw: str) -> ToolOutput:
    return ToolOutput(
        success=len(issues) == 0,
        data={"file": str(path), "issues": issues, "fixed": fixed, "raw_output": raw},
    )


def _lint_python(path: Path, fix: bool) -> ToolOutput:
    root = find_project_root(path)
    ruff = _resolve_exe(root, "ruff")
    if ruff is None:
        return _skipped(path, "ruff is not installed in this project; skipping lint.")
    rel = _rel(path, root)
    if fix:
        _run([ruff, "check", "--fix", rel], root)
        _run([ruff, "format", rel], root)
    out, _, err = _run([ruff, "check", "--output-format=concise", rel], root)
    if err:
        return ToolOutput(success=False, error=err)
    fmt_out, fmt_code, _ = _run([ruff, "format", "--check", rel], root)
    issues = [line.strip() for line in out.splitlines() if re.search(r":\d+:\d+:", line)]
    if fmt_code != 0 and "would reformat" in fmt_out.lower():
        issues.append(f"{rel}: not formatted (run with fix=true)")
    return _result(path, issues, fix, out + fmt_out)


def _lint_eslint(path: Path, fix: bool) -> ToolOutput:
    root = find_project_root(path, markers=("package.json", ".eslintrc", ".eslintrc.json", ".git"))
    eslint = _resolve_exe(root, "eslint") or (["npx", "--no-install", "eslint"] if shutil.which("npx") else None)
    if eslint is None:
        return _skipped(path, "No local eslint found; skipping lint.")
    cmd = eslint if isinstance(eslint, list) else [eslint]
    if fix:
        _run([*cmd, "--fix", str(path)], root)
    # The `json` formatter is the only structured one built into every ESLint 8/9
    # release (unix/compact/etc. were removed from core in v9), so parse that.
    out, _, err = _run([*cmd, "--format", "json", str(path)], root)
    if err:
        return ToolOutput(success=False, error=err)
    issues = _parse_eslint_json(out)
    if issues is None:
        return _skipped(path, "eslint produced no parseable output; skipping.")
    return _result(path, issues, fix, out)


def _parse_eslint_json(out: str) -> list[str] | None:
    """Extract 'file:line:col: message (rule)' issue strings from eslint --format json."""
    import json

    start = out.find("[")
    if start == -1:
        return None
    try:
        results = json.loads(out[start:])
    except json.JSONDecodeError:
        return None
    issues: list[str] = []
    for file_result in results:
        file_path = file_result.get("filePath", "")
        for msg in file_result.get("messages", []):
            rule = f" ({msg['ruleId']})" if msg.get("ruleId") else ""
            issues.append(f"{file_path}:{msg.get('line', 0)}:{msg.get('column', 0)}: {msg.get('message', '')}{rule}")
    return issues


def _lint_go(path: Path, fix: bool) -> ToolOutput:
    if not shutil.which("gofmt"):
        return _skipped(path, "gofmt not found; skipping lint.")
    root = find_project_root(path, markers=("go.mod", ".git"))
    if fix:
        _run(["gofmt", "-w", str(path)], root)
    out, _, err = _run(["gofmt", "-l", str(path)], root)
    if err:
        return ToolOutput(success=False, error=err)
    issues = [f"{line.strip()}: not gofmt-formatted (run with fix=true)" for line in out.splitlines() if line.strip()]
    return _result(path, issues, fix, out)
