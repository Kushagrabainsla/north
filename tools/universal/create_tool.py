"""Tool manager - create, update, list, and hot-reload north tools at runtime."""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import inspect
import re
import textwrap
from pathlib import Path
from typing import TYPE_CHECKING, Any

from approval.policy import Action, ActionKind
from policies.self_edit import SelfEditPolicy
from tools.base import ApprovalGatedTool, Tool
from tools.models import ToolInput, ToolOutput
from tools.specialized._approval import gate_action

if TYPE_CHECKING:
    from approval.base import Notifier
    from approval.policy import ApprovalPolicy
    from approval.store import ApprovalStore
    from tools.registry import ToolRegistry
    from utils.events import EventEmitter

_TOOLS_ROOT = Path(__file__).parent.parent

# Code shown in the approval card before truncation.
_PREVIEW_CHARS = 1_500

_NAME_RE = re.compile(r'^\s+name\s*=\s*["\']([^"\']+)["\']', re.MULTILINE)
_DESC_RE = re.compile(r'^\s+description\s*=\s*[\(\s]*["\']([^"\']+)["\']', re.MULTILINE)


class CreateToolTool(ApprovalGatedTool):
    """Creates, updates, or lists north tools so agents can extend their own capabilities."""

    name = "create_tool"
    is_mutating = True
    description = (
        "Create, update, inspect, validate, test, and activate atomic executable capabilities. "
        "Use the adding-a-north-tool skill first and call action='list' before creating anything. "
        "New and edited tools are non-runnable candidates. They enter the live catalog only after "
        "structural validation, a successful explicit test call, and user-confirmed activation."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "update", "read", "list", "validate", "test", "activate"],
                "description": (
                    "list = show all tools with location and description; "
                    "read = return full source of an existing tool by name; "
                    "create = write a new tool file (provide 'content' for a full implementation); "
                    "update = stage improved code without replacing the active tool; "
                    "validate = import and inspect the candidate; test = call it with test_params; "
                    "activate = publish the tested candidate after explicit user confirmation"
                ),
            },
            "name": {
                "type": "string",
                "description": "Tool name in snake_case. Required for create, update, and read.",
            },
            "description": {
                "type": "string",
                "description": "What the tool does. Required for create (when content is not provided).",
            },
            "content": {
                "type": "string",
                "description": (
                    "Full Python source for the tool file. MUST define a subclass of Tool (from tools.base) "
                    "with name, description, parameters_schema, format_output, and async def run. Example:\n"
                    "from tools.base import Tool\n"
                    "from tools.models import ToolInput, ToolOutput\n"
                    "class MyTool(Tool):\n"
                    "    name = 'my_tool'\n"
                    "    description = '...'\n"
                    "    parameters_schema = {'type': 'object', 'properties': {}}\n"
                    "    def format_output(self, data: dict) -> str:\n"
                    "        return 'success'\n"
                    "    async def run(self, input: ToolInput) -> ToolOutput:\n"
                    "        return ToolOutput(success=True, data={})"
                ),
            },
            "tool_type": {
                "type": "string",
                "enum": ["universal", "specialized"],
                "description": (
                    "Code organization directory only. Both values join the same global tool catalog. "
                    "Default: specialized."
                ),
            },
            "parameters": {
                "type": "array",
                "description": "Input parameters. Used when generating a stub (action=create without content).",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "type": {
                            "type": "string",
                            "enum": ["string", "integer", "number", "boolean", "array", "object"],
                        },
                        "description": {"type": "string"},
                        "required": {"type": "boolean"},
                    },
                    "required": ["name", "type", "description"],
                },
            },
            "implementation_notes": {
                "type": "string",
                "description": "Hints about implementation. Used when generating a stub.",
            },
            "test_params": {
                "type": "object",
                "description": "Small, safe representative input passed to the candidate during action=test.",
            },
            "user_confirmed": {
                "type": "boolean",
                "description": "True only after the user explicitly approves activation.",
            },
        },
        "required": ["action"],
    }

    def __init__(
        self,
        tool_registry: ToolRegistry | None = None,
        approval_store: ApprovalStore | None = None,
        stream_manager: EventEmitter | None = None,
        approval_timeout_seconds: float = 300.0,
        policy: ApprovalPolicy | None = None,
        notifier: Notifier | None = None,
        self_edit_policy: SelfEditPolicy | None = None,
        tools_dir: Path | None = None,
    ) -> None:
        super().__init__(approval_store, stream_manager, approval_timeout_seconds, policy, notifier)
        self._registry = tool_registry
        self._self_edit_policy = self_edit_policy
        self._tools_dir = tools_dir or _TOOLS_ROOT
        self._validated: dict[str, str] = {}
        self._tested: dict[str, str] = {}

    def mutates(self, params: dict[str, Any] | None = None) -> bool:
        return str((params or {}).get("action") or "create").strip().lower() not in {"list", "read"}

    def format_output(self, data: dict[str, Any]) -> str:
        action = data.get("action")

        if action == "list":
            rows = data.get("tools", [])
            if not rows:
                return "No tools found."
            return "\n".join(
                f"[{r['type']} · {r.get('status', 'active')}] {r['name']} - {r['description']}"
                for r in rows
            )

        if action == "read":
            return data.get("content", "(empty)")

        if action == "create":
            return (
                f"Tool candidate created: {data['path']}\n"
                "It is not live until validation and a representative test pass, then the user confirms activation."
            )

        if action == "update":
            return (
                f"Tool update candidate staged: {data['path']}\n"
                "The currently active implementation is unchanged until this candidate is tested and activated."
            )

        if action == "validate":
            return f"Tool candidate '{data['name']}' passed structural validation."

        if action == "test":
            return f"Tool candidate '{data['name']}' passed its representative test call."

        if action == "activate":
            return f"Tool '{data['name']}' is active in the live catalog."

        return str(data)

    async def run(self, input: ToolInput) -> ToolOutput:
        action = (input.params.get("action") or "create").strip()

        if action == "list":
            return _list_tools(self._tools_dir)
        if action == "read":
            return _read_tool(input.params.get("name") or "", self._tools_dir)
        if action in {"create", "update", "validate", "test", "activate"}:
            # Fail closed: model-authored code may be imported or hot-loaded,
            # so every lifecycle transition that executes or publishes it is
            # visible to the user approval system.
            refused = await self._gate(input.params, action)
            if refused is not None:
                return refused
            if action == "create":
                return self._create(input.params)
            if action == "update":
                return self._update(input.params)
            if action == "validate":
                return self._validate(input.params)
            if action == "test":
                return await self._test(input.params)
            return self._activate(input.params)

        return ToolOutput(
            success=False,
            error=f"Unknown action '{action}'. Use: list, read, create, update, validate, test, activate.",
        )

    async def _gate(self, params: dict, action: str) -> ToolOutput | None:
        """Show the proposed code and wait. ``None`` when it may be written."""
        name = params.get("name", "unknown")
        tool_type = params.get("tool_type", "specialized")
        content = (params.get("content") or "").strip()
        if not content and name:
            candidate = _find_candidate_path(str(name), self._tools_dir)
            if candidate is not None:
                content = candidate.read_text(encoding="utf-8")
        preview = (content[:_PREVIEW_CHARS] + "\n…") if len(content) > _PREVIEW_CHARS else content
        message = f"Agent wants to {action} the '{name}' tool ({tool_type}).\n\n" + (
            f"```python\n{preview}\n```" if preview else "(stub - no implementation provided)"
        )
        return await gate_action(
            Action(
                agent="create_tool",
                kind=ActionKind.TOOL_CHANGE,
                summary=f"{action} the {name!r} tool ({tool_type})",
                operation=action,
                args=name,
            ),
            policy=self._policy,
            approval_store=self._approval_store,
            title="Tool Change - Approval Required",
            message=message,
            options=("Approve", "Reject"),
            task_id=params.get("task_id"),
            stream_manager=self._stream_manager,
            notifier=self._notifier,
            timeout=self._approval_timeout_seconds,
            declined=f"Tool {action} cancelled by user.",
        )

    # ── Action handlers ───────────────────────────────────────────────────────

    def _create(self, params: dict) -> ToolOutput:
        tool_name = (params.get("name") or "").strip()
        description = (params.get("description") or "").strip()
        content = (params.get("content") or "").strip()
        tool_type = params.get("tool_type") or "specialized"
        parameters = params.get("parameters") or []
        notes = (params.get("implementation_notes") or "").strip()

        if not tool_name:
            return ToolOutput(success=False, error="Parameter 'name' is required for action=create.")
        if not re.match(r"^[a-z][a-z0-9_]*$", tool_name):
            return ToolOutput(success=False, error="Tool name must be snake_case (lowercase, digits, underscores).")
        if not content and not description:
            return ToolOutput(success=False, error="Either 'content' or 'description' is required for action=create.")
        if tool_type not in ("universal", "specialized"):
            return ToolOutput(success=False, error="tool_type must be 'universal' or 'specialized'.")

        target_dir = _candidate_root(self._tools_dir) / tool_type
        target_dir.mkdir(parents=True, exist_ok=True)

        file_path = target_dir / f"{tool_name}.py"
        registered = bool(self._registry is not None and tool_name in self._registry.all_tool_names())
        if file_path.exists() or _find_active_tool_path(tool_name, self._tools_dir) is not None or registered:
            return ToolOutput(
                success=False,
                error=(
                    f"Tool '{tool_name}' already exists. "
                    "Use action='read' to inspect it, then action='update' to extend it."
                ),
            )

        if not content:
            content = _render_stub(
                tool_name=tool_name,
                class_name=_to_class_name(tool_name),
                description=description,
                parameters=parameters,
                notes=notes,
            )

        safe, reason = _check_code_safety(content)
        if not safe:
            return ToolOutput(
                success=False,
                error=f"Tool code rejected by static safety check: {reason}. Remove the flagged pattern and try again.",
            )

        mutation = None
        if self._self_edit_policy is not None:
            try:
                mutation = self._self_edit_policy.begin(file_path, "create")
            except PermissionError as exc:
                return ToolOutput(success=False, error=str(exc))
        file_path.write_text(content, encoding="utf-8")
        if mutation is not None:
            self._self_edit_policy.commit(mutation)

        self._validated.pop(tool_name, None)
        self._tested.pop(tool_name, None)

        return ToolOutput(
            success=True,
            data={
                "action": "create",
                "path": str(file_path),
                "status": "candidate",
            },
        )

    def _update(self, params: dict) -> ToolOutput:
        tool_name = (params.get("name") or "").strip()
        content = (params.get("content") or "").strip()

        if not tool_name:
            return ToolOutput(success=False, error="Parameter 'name' is required for action=update.")
        if not content:
            return ToolOutput(
                success=False,
                error="Parameter 'content' (full updated Python source) is required for action=update.",
            )

        source_path = _find_tool_path(tool_name, self._tools_dir)
        if source_path is None:
            return ToolOutput(
                success=False,
                error=f"Tool '{tool_name}' not found. Use action='create' to create a new tool.",
            )

        if f'name = "{tool_name}"' not in content and f"name = '{tool_name}'" not in content:
            return ToolOutput(
                success=False,
                error=f"Updated content must keep 'name = \"{tool_name}\"' - tool name cannot change.",
            )

        safe, reason = _check_code_safety(content)
        if not safe:
            return ToolOutput(
                success=False,
                error=f"Tool code rejected by static safety check: {reason}. Remove the flagged pattern and try again.",
            )

        tool_type = "universal" if source_path.parent.name == "universal" else "specialized"
        path = _candidate_root(self._tools_dir) / tool_type / f"{tool_name}.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        mutation = None
        if self._self_edit_policy is not None:
            try:
                mutation = self._self_edit_policy.begin(path, "update" if path.exists() else "create")
            except PermissionError as exc:
                return ToolOutput(success=False, error=str(exc))
        path.write_text(content, encoding="utf-8")
        if mutation is not None:
            self._self_edit_policy.commit(mutation)

        self._validated.pop(tool_name, None)
        self._tested.pop(tool_name, None)

        return ToolOutput(
            success=True,
            data={
                "action": "update",
                "path": str(path),
                "status": "candidate",
            },
        )

    def _validate(self, params: dict) -> ToolOutput:
        name = str(params.get("name") or "").strip()
        if not name:
            return ToolOutput(success=False, error="Parameter 'name' is required for action=validate.")
        path = _find_candidate_path(name, self._tools_dir)
        if path is None:
            return ToolOutput(success=False, error=f"No candidate exists for tool '{name}'.")
        try:
            instance = _load_tool_file(path, expected_name=name)
            _validate_tool_contract(instance)
        except Exception as exc:
            return ToolOutput(success=False, error=f"Tool candidate validation failed: {exc}")
        fingerprint = _tool_fingerprint(path)
        self._validated[name] = fingerprint
        self._tested.pop(name, None)
        return ToolOutput(
            success=True,
            data={"action": "validate", "name": name, "status": "candidate", "valid": True},
        )

    async def _test(self, params: dict) -> ToolOutput:
        name = str(params.get("name") or "").strip()
        test_params = params.get("test_params")
        if not name:
            return ToolOutput(success=False, error="Parameter 'name' is required for action=test.")
        if not isinstance(test_params, dict):
            return ToolOutput(success=False, error="Parameter 'test_params' must be an object for action=test.")
        path = _find_candidate_path(name, self._tools_dir)
        if path is None:
            return ToolOutput(success=False, error=f"No candidate exists for tool '{name}'.")
        fingerprint = _tool_fingerprint(path)
        if self._validated.get(name) != fingerprint:
            return ToolOutput(success=False, error="Validate the current tool candidate before testing it.")
        try:
            instance = _load_tool_file(path, expected_name=name)
            _validate_tool_contract(instance)
            result = await instance.run(ToolInput(params=test_params))
        except Exception as exc:
            return ToolOutput(success=False, error=f"Tool candidate test raised an exception: {exc}")
        if not isinstance(result, ToolOutput):
            return ToolOutput(success=False, error="Tool candidate test did not return ToolOutput.")
        if not result.success:
            return ToolOutput(
                success=False,
                error=result.error or "Tool candidate returned an unsuccessful test result.",
                data={"candidate_output": result.data},
            )
        self._tested[name] = fingerprint
        return ToolOutput(
            success=True,
            data={
                "action": "test",
                "name": name,
                "status": "candidate",
                "candidate_output": result.data,
            },
        )

    def _activate(self, params: dict) -> ToolOutput:
        name = str(params.get("name") or "").strip()
        if not name:
            return ToolOutput(success=False, error="Parameter 'name' is required for action=activate.")
        if params.get("user_confirmed") is not True:
            return ToolOutput(success=False, error="Activation requires explicit user confirmation.")
        if self._registry is None:
            return ToolOutput(success=False, error="The live tool registry is unavailable.")
        candidate = _find_candidate_path(name, self._tools_dir)
        if candidate is None:
            return ToolOutput(success=False, error=f"No candidate exists for tool '{name}'.")
        fingerprint = _tool_fingerprint(candidate)
        if self._tested.get(name) != fingerprint:
            return ToolOutput(success=False, error="Test the current tool candidate successfully before activation.")
        try:
            instance = _load_tool_file(candidate, expected_name=name)
            _validate_tool_contract(instance)
            tool_type = candidate.parent.name
            target = self._tools_dir / tool_type / f"{name}.py"
            target.parent.mkdir(parents=True, exist_ok=True)
            mutation = (
                self._self_edit_policy.begin(target, "update" if target.exists() else "create")
                if self._self_edit_policy is not None
                else None
            )
            target.write_text(candidate.read_text(encoding="utf-8"), encoding="utf-8")
            if mutation is not None:
                self._self_edit_policy.commit(mutation)
            self._registry.register(instance)
            candidate.unlink()
        except Exception as exc:
            return ToolOutput(success=False, error=f"Failed to activate tool '{name}': {exc}")
        self._validated.pop(name, None)
        self._tested.pop(name, None)
        return ToolOutput(
            success=True,
            data={"action": "activate", "name": name, "path": str(target), "status": "active"},
        )


# ── Code safety ──────────────────────────────────────────────────────────────

def _check_code_safety(code: str) -> tuple[bool, str]:
    """Validate syntax before a trusted, approval-gated learned tool is loaded.

    Learned tools are trusted extensions and intentionally retain normal Python
    capabilities, including filesystem, process, network, and reflection APIs.
    The approval card and self-edit path policy are the control points; this
    helper only prevents malformed source from reaching them.
    """
    try:
        ast.parse(code)
    except SyntaxError as exc:
        return False, f"Syntax error: {exc}"
    return True, ""


# ── Standalone helpers ────────────────────────────────────────────────────────


def _list_tools(learned_root: Path = _TOOLS_ROOT) -> ToolOutput:
    rows_by_name: dict[str, dict[str, Any]] = {}
    roots = ((_TOOLS_ROOT, "builtin"), (learned_root, "learned"))
    for root, source in roots:
        for kind in ("universal", "specialized"):
            directory = root / kind
            if not directory.exists():
                continue
            for path in sorted(directory.glob("*.py")):
                if path.name.startswith("_"):
                    continue
                source_text = path.read_text(encoding="utf-8")
                name_m = _NAME_RE.search(source_text)
                desc_m = _DESC_RE.search(source_text)
                name = name_m.group(1) if name_m else path.stem
                rows_by_name[name] = {
                    "name": name,
                    "type": kind,
                    "source": source,
                    "status": "active",
                    "description": desc_m.group(1) if desc_m else "(no description)",
                    "path": str(path),
                }
    candidate_root = _candidate_root(learned_root)
    for kind in ("universal", "specialized"):
        directory = candidate_root / kind
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.py")):
            source_text = path.read_text(encoding="utf-8")
            name_m = _NAME_RE.search(source_text)
            desc_m = _DESC_RE.search(source_text)
            name = name_m.group(1) if name_m else path.stem
            rows_by_name[name] = {
                "name": name,
                "type": kind,
                "source": "learned",
                "status": "candidate",
                "description": desc_m.group(1) if desc_m else "(no description)",
                "path": str(path),
            }
    return ToolOutput(
        success=True,
        data={"action": "list", "tools": sorted(rows_by_name.values(), key=lambda row: row["name"])},
    )


def _read_tool(tool_name: str, learned_root: Path = _TOOLS_ROOT) -> ToolOutput:
    if not tool_name.strip():
        return ToolOutput(success=False, error="Parameter 'name' is required for action=read.")
    path = _find_tool_path(tool_name.strip(), learned_root)
    if path is None:
        return ToolOutput(success=False, error=f"Tool '{tool_name}' not found.")
    return ToolOutput(
        success=True,
        data={
            "action": "read",
            "name": tool_name,
            "path": str(path),
            "content": path.read_text(encoding="utf-8"),
        },
    )

def _candidate_root(learned_root: Path) -> Path:
    return learned_root / "candidates"


def _find_candidate_path(tool_name: str, learned_root: Path = _TOOLS_ROOT) -> Path | None:
    for kind in ("universal", "specialized"):
        path = _candidate_root(learned_root) / kind / f"{tool_name}.py"
        if path.exists():
            return path
    return None


def _find_active_tool_path(tool_name: str, learned_root: Path = _TOOLS_ROOT) -> Path | None:
    for root in (learned_root, _TOOLS_ROOT):
        for kind in ("universal", "specialized"):
            p = root / kind / f"{tool_name}.py"
            if p.exists():
                return p
    return None


def _find_tool_path(tool_name: str, learned_root: Path = _TOOLS_ROOT) -> Path | None:
    return _find_candidate_path(tool_name, learned_root) or _find_active_tool_path(tool_name, learned_root)


def _tool_fingerprint(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_tool_file(path: Path, *, expected_name: str) -> Tool:
    """Load one candidate without exposing it through the live registry."""
    module_name = f"north_tool_candidate_{expected_name}_{_tool_fingerprint(path)[:12]}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ValueError("Python could not create an import specification")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    matches: list[Tool] = []
    for obj in vars(module).values():
        if not isinstance(obj, type) or not issubclass(obj, Tool) or obj is Tool or inspect.isabstract(obj):
            continue
        if _needs_constructor_args(obj):
            raise ValueError(f"{obj.__name__} requires constructor arguments and cannot be auto-loaded")
        instance = obj()
        if instance.name == expected_name:
            matches.append(instance)
    if len(matches) != 1:
        raise ValueError(f"expected exactly one concrete Tool named {expected_name!r}, found {len(matches)}")
    return matches[0]


def _needs_constructor_args(tool_cls: type[Tool]) -> bool:
    signature = inspect.signature(tool_cls)
    parameter_kinds = {
        inspect.Parameter.POSITIONAL_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.KEYWORD_ONLY,
    }
    return any(
        parameter.default is inspect.Parameter.empty and parameter.kind in parameter_kinds
        for parameter in signature.parameters.values()
    )


def _validate_tool_contract(tool: Tool) -> None:
    if not tool.name or not re.fullmatch(r"[a-z][a-z0-9_]*", tool.name):
        raise ValueError("tool name must use snake_case")
    if not isinstance(tool.description, str) or not tool.description.strip():
        raise ValueError("tool description must be non-empty")
    schema = tool.parameters_schema
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise ValueError("parameters_schema must be an object schema")
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        raise ValueError("parameters_schema.properties must be an object")
    required = schema.get("required", [])
    if not isinstance(required, list) or any(name not in properties for name in required):
        raise ValueError("every required parameter must be declared in properties")


def _to_class_name(snake: str) -> str:
    return "".join(w.title() for w in snake.split("_")) + "Tool"


def _render_schema(parameters: list[dict]) -> str:
    if not parameters:
        return '{"type": "object", "properties": {}}'

    prop_lines, required_names = [], []
    for p in parameters:
        p_desc = p.get("description", "").replace('"', '\\"')
        entry = f'            "{p["name"]}": {{"type": "{p.get("type", "string")}", "description": "{p_desc}"}},'
        prop_lines.append(entry)
        if p.get("required", True):
            required_names.append(p["name"])

    props = "\n".join(prop_lines)
    suffix = f'        "required": {repr(required_names)},\n    }}' if required_names else "    }"
    return f'{{\n        "type": "object",\n        "properties": {{\n{props}\n        }},\n{suffix}'


def _render_param_extraction(parameters: list[dict]) -> str:
    if not parameters:
        return "        pass  # no parameters defined"
    lines = []
    for p in parameters:
        name = p["name"]
        lines.append(f'        {name} = input.params.get("{name}")')
        if p.get("required", True):
            lines.append(f"        if {name} is None:")
            lines.append(f"            return ToolOutput(success=False, error=\"Parameter '{name}' is required.\")")
    return "\n".join(lines)


def _render_stub(tool_name: str, class_name: str, description: str, parameters: list[dict], notes: str) -> str:
    desc_escaped = description.replace('"', '\\"')
    notes_block = ""
    if notes:
        wrapped = textwrap.fill(notes, width=76, initial_indent="        # ", subsequent_indent="        # ")
        notes_block = f"\n        # Implementation notes:\n{wrapped}\n"

    return (
        f'"""Auto-generated tool stub - {tool_name}.\n\nEdit this file to implement the tool logic.\n"""\n\n'
        "from __future__ import annotations\n\n"
        "from typing import Any\n\n"
        "from tools.base import Tool\n"
        "from tools.models import ToolInput, ToolOutput\n\n\n"
        f"class {class_name}(Tool):\n"
        f'    """{description}"""\n\n'
        f'    name = "{tool_name}"\n'
        f'    description = (\n        "{desc_escaped}"\n    )\n'
        f"    parameters_schema = {_render_schema(parameters)}\n\n"
        "    def format_output(self, data: dict[str, Any]) -> str:\n"
        '        return str(data.get("result", data))\n\n'
        "    async def run(self, input: ToolInput) -> ToolOutput:\n"
        f"{_render_param_extraction(parameters)}\n"
        f"{notes_block}"
        "        # TODO: implement tool logic here\n"
        "        raise NotImplementedError\n"
    )
