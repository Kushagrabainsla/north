"""The mistakes a model makes writing a north tool, found before the draft's test call.

Each problem says exactly what to change, so the model fixes the draft in one more call
instead of guessing from a failed test or asking the user. Only mistakes with one clear
fix are checked: a model-written tool is otherwise trusted code (see `_check_code_safety`).
"""

from __future__ import annotations

import ast
import importlib.util

# What a tool may read off its ToolInput; anything else is a guess at how arguments arrive.
_INPUT_FIELDS = {"params", "approved", "granted_workspace", "edit_scope", "model_copy"}
_INSTALLED_HINT = (
    "Use the standard library or a package north already has: httpx, bs4, pydantic, yaml, numpy, psutil, "
    "PIL, pypdf, docx. North cannot install packages for a custom tool."
)
# Calls that wait without letting north run anything else, and what to use instead.
_BLOCKING = {
    ("time", "sleep"): "await asyncio.sleep(...)",
    ("urllib.request", "urlopen"): "httpx.AsyncClient(timeout=...)",
    ("subprocess", "run"): "await asyncio.to_thread(subprocess.run, ...)",
    ("subprocess", "call"): "await asyncio.to_thread(subprocess.call, ...)",
    ("subprocess", "check_output"): "await asyncio.to_thread(subprocess.check_output, ...)",
    ("subprocess", "check_call"): "await asyncio.to_thread(subprocess.check_call, ...)",
}
# Calls that change something outside the tool: a tool making them must say so, or north never asks.
_CHANGING_METHODS = {"post", "put", "patch", "delete", "write_text", "write_bytes", "unlink", "rmdir", "rename"}
_CHANGING_FUNCTIONS = {
    ("os", "remove"),
    ("os", "unlink"),
    ("os", "rename"),
    ("os", "replace"),
    ("os", "rmdir"),
    ("os", "system"),
    ("shutil", "rmtree"),
    ("shutil", "move"),
    ("shutil", "copy"),
    ("shutil", "copyfile"),
    ("subprocess", "run"),
    ("subprocess", "Popen"),
    ("subprocess", "call"),
}


def draft_problems(source: str) -> list[str]:
    """What must change in *source*, a tool draft, before it is worth testing; [] when nothing."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [f"It does not parse: {exc}."]
    problems = _imports(tree)
    for tool in [node for node in tree.body if isinstance(node, ast.ClassDef) and _is_tool(node)]:
        problems += _tool_class(tool)
    problems += _environment_and_paths(tree)
    return problems


def _is_tool(node: ast.ClassDef) -> bool:
    return any(
        (isinstance(base, ast.Name) and base.id == "Tool") or (isinstance(base, ast.Attribute) and base.attr == "Tool")
        for base in node.bases
    )


def _imports(tree: ast.Module) -> list[str]:
    problems: list[str] = []
    for node in ast.walk(tree):
        names = [alias.name for alias in node.names] if isinstance(node, ast.Import) else []
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module]
        for name in names:
            top = name.split(".")[0]
            if top != "__future__" and importlib.util.find_spec(top) is None:
                instead = " Use httpx.AsyncClient, which is installed." if top == "requests" else ""
                problems.append(f"It imports {top!r}, which north does not have.{instead} {_INSTALLED_HINT}")
    return problems


def _tool_class(tool: ast.ClassDef) -> list[str]:
    problems: list[str] = []
    methods = {node.name: node for node in tool.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    run = methods.get("run")
    if run is None:
        return [f"{tool.name} has no run method: add `async def run(self, input: ToolInput) -> ToolOutput`."]
    if isinstance(run, ast.FunctionDef):
        problems.append(f"{tool.name}.run must be `async def run(self, input: ToolInput) -> ToolOutput`.")
    arguments = [arg.arg for arg in run.args.args]
    argument = arguments[1] if len(arguments) > 1 else "input"
    body = list(_own_nodes(run))
    for node in body:
        reads_input = isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == argument
        if reads_input and node.attr not in _INPUT_FIELDS:
            problems.append(
                f"run reads `{argument}.{node.attr}`: the call's arguments are the dict `{argument}.params`."
            )
        if isinstance(node, ast.Raise):
            problems.append(
                "run raises an exception: return `ToolOutput(success=False, error=...)` saying what to change "
                "instead, so the agent can fix the call."
            )
    for method in methods.values():
        if isinstance(method, ast.AsyncFunctionDef):
            problems += _blocking(method)
    declared = "mutates" in methods or any(
        isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "is_mutating" for target in node.targets)
        and isinstance(node.value, ast.Constant)
        and node.value.value is True
        for node in tool.body
    )
    if not declared and (change := _first_change(tool)):
        problems.append(
            f"It changes something (it calls `{change}`) but says it only reads: set `is_mutating = True` so north "
            "asks before it runs, or override `mutates(params)` if only some calls change things."
        )
    return list(dict.fromkeys(problems))


def _own_nodes(function: ast.AST):
    """The nodes of *function*'s own body, not of functions defined inside it."""
    for child in ast.iter_child_nodes(function):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        yield child
        yield from _own_nodes(child)


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value)}.{node.attr}"
    return ""


def _blocking(method: ast.AsyncFunctionDef) -> list[str]:
    problems = []
    for node in _own_nodes(method):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            owner, name = _dotted(node.func.value), node.func.attr
            if owner == "requests":
                continue  # reported with the import
            if (owner, name) in _BLOCKING:
                problems.append(
                    f"{method.name} calls `{owner}.{name}`, which freezes north while it waits: use "
                    f"{_BLOCKING[(owner, name)]}."
                )
    return problems


def _first_change(tool: ast.ClassDef) -> str:
    for node in ast.walk(tool):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute):
            owner, name = _dotted(node.func.value), node.func.attr
            if (owner, name) in _CHANGING_FUNCTIONS or name in _CHANGING_METHODS:
                return f"{owner}.{name}" if owner else name
        if isinstance(node.func, ast.Name) and node.func.id == "open" and _opens_to_write(node):
            return "open(..., 'w')"
    return ""


def _opens_to_write(call: ast.Call) -> bool:
    mode = call.args[1] if len(call.args) > 1 else next((k.value for k in call.keywords if k.arg == "mode"), None)
    return isinstance(mode, ast.Constant) and isinstance(mode.value, str) and any(c in mode.value for c in "wax+")


def _environment_and_paths(tree: ast.Module) -> list[str]:
    problems: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and _dotted(node) == "os.environ"
            or (isinstance(node, ast.Call) and _dotted(node.func) == "os.getenv")
        ):
            problems.append(
                "It reads environment variables, which north does not pass its settings through: use "
                "`from config.settings import settings` for north's own values. North has no place yet for a "
                "custom tool's key; if one is needed, stop and tell the user."
            )
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value
            if "~/.north" in text or "/.north/" in text or text.startswith(("/Users/", "/home/")):
                problems.append(
                    f"It names a fixed path ({text!r}): build paths from `settings.north_home`, such as "
                    "settings.north_home / 'tool-data' / '<tool name>', so it works on every install."
                )
    return list(dict.fromkeys(problems))
