"""north's home is wherever NORTH_HOME says, and only `settings.north_home` says where.

A path built from `~/.north` is right only on a default install. With NORTH_HOME
set (Docker's /data, a second install, a test run) it pointed at the wrong
folder: settings read keys from a file the installer never wrote, the path guard
left the real home open, and the general agent was told to save notes in a folder
north itself blocks.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

from config.settings import Settings

ROOT = Path(__file__).parents[3]

# Code that may still read the variable or name the default home, and why.
_MAY_READ_NORTH_HOME = {
    "config/settings.py": "the one place NORTH_HOME is read",
    "utils/handoff.py": "a platform utility that may not import config; it reads the same variable",
}
_MAY_NAME_DEFAULT_HOME = {
    "config/settings.py": "the default when NORTH_HOME is unset",
    "utils/handoff.py": "the same default, for the same reason",
    "tools/_path.py": "a default install's home stays blocked while another home is in use",
    "coding_agents/constants.py": "the same, for what a coding agent may never read",
    "tools/universal/_draft_checks.py": "finds a tool draft that writes the default home into its code",
}
# What the model reads: agent prompts and the bundled skills.
_MODEL_FACING = ("agents/**/prompts/*.md", "resources/**/*.md")
# A shell default that still honours NORTH_HOME.
_SHELL_DEFAULT = "${NORTH_HOME:-$HOME/.north}"


def _tracked(pattern: str) -> list[str]:
    listed = subprocess.run(["git", "-C", str(ROOT), "ls-files", pattern], capture_output=True, text=True, check=True)
    return [line for line in listed.stdout.splitlines() if line]


def _docstrings(tree: ast.AST) -> set[int]:
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                found.add(id(first.value))
    return found


def _reads_north_home(node: ast.AST) -> bool:
    """`os.environ.get("NORTH_HOME")`, `os.getenv("NORTH_HOME")` or `os.environ["NORTH_HOME"]`."""
    if isinstance(node, ast.Call) and node.args and isinstance(node.func, ast.Attribute):
        first = node.args[0]
        return node.func.attr in {"get", "getenv"} and isinstance(first, ast.Constant) and first.value == "NORTH_HOME"
    if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load):
        return isinstance(node.slice, ast.Constant) and node.slice.value == "NORTH_HOME"
    return False


def _joins_home_and_dot_north(node: ast.AST) -> bool:
    """`Path.home() / ".north"`."""
    return (
        isinstance(node, ast.BinOp)
        and isinstance(node.op, ast.Div)
        and isinstance(node.right, ast.Constant)
        and node.right.value == ".north"
        and isinstance(node.left, ast.Call)
        and isinstance(node.left.func, ast.Attribute)
        and node.left.func.attr == "home"
    )


def _places_that_decide_where_home_is() -> list[str]:
    found = []
    for rel in _tracked("*.py"):
        if rel.startswith(("tests/", "experiments/")):
            continue
        tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
        docstrings = _docstrings(tree)
        for node in ast.walk(tree):
            if _reads_north_home(node) and rel not in _MAY_READ_NORTH_HOME:
                found.append(f"{rel}:{node.lineno} reads NORTH_HOME")
            if _joins_home_and_dot_north(node):
                found.append(f"{rel}:{node.lineno} builds Path.home() / '.north'")
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstrings
                and "~/.north" in node.value
                and rel not in _MAY_NAME_DEFAULT_HOME
            ):
                found.append(f"{rel}:{node.lineno} names ~/.north: {node.value[:60]!r}")
    return found


def _names_default_home(text: str) -> bool:
    text = text.replace(_SHELL_DEFAULT, "")
    return "~/.north" in text or "$HOME/.north" in text


def test_only_settings_decides_where_north_home_is() -> None:
    assert _places_that_decide_where_home_is() == []


def test_no_prompt_or_skill_tells_the_model_its_home_is_tilde_north() -> None:
    told = [
        rel for pattern in _MODEL_FACING for rel in _tracked(pattern) if _names_default_home((ROOT / rel).read_text())
    ]
    assert told == []


def test_settings_read_the_env_file_in_north_home(monkeypatch, tmp_path: Path) -> None:
    # Where the installer, `north setup` and the dashboard save keys.
    (tmp_path / ".env").write_text("NORTH_GEMINI_API_KEY=saved-in-north-home\n", encoding="utf-8")
    monkeypatch.setenv("NORTH_HOME", str(tmp_path))
    monkeypatch.delenv("NORTH_GEMINI_API_KEY", raising=False)

    assert Settings().gemini_api_key == "saved-in-north-home"


def test_settings_take_north_home_from_the_environment_when_created(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("NORTH_HOME", str(tmp_path))

    assert Settings(_env_file=None).north_home == tmp_path
