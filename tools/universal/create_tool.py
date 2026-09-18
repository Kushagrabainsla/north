"""Tool manager - create, update, list, and hot-reload north tools at runtime."""

from __future__ import annotations

import ast
import importlib
import inspect
import re
import sys
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
        "Last-resort tool manager - only use this when no existing tool can perform the required action. "
        "Always call action='list' first to check what tools exist before creating anything. "
        "action='list': show all tools with descriptions. "
        "action='read': return full source of a tool by name. "
        "action='update': extend an existing tool with new behaviour "
        "(preferred over creating a new one when a similar tool exists). "
        "action='create': write a brand-new tool - "
        "provide full working Python in 'content' so it is immediately usable. "
        "Hot-loads into the running server so the new tool is available in the very next step."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "update", "read", "list"],
                "description": (
                    "list = show all tools with location and description; "
                    "read = return full source of an existing tool by name; "
                    "create = write a new tool file (provide 'content' for a full implementation); "
                    "update = overwrite an existing tool with improved code, keeping existing behaviour"
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

    def format_output(self, data: dict[str, Any]) -> str:
        action = data.get("action")

        if action == "list":
            rows = data.get("tools", [])
            if not rows:
                return "No tools found."
            return "\n".join(f"[{r['type']}] {r['name']} - {r['description']}" for r in rows)

        if action == "read":
            return data.get("content", "(empty)")

        if action == "create":
            lines = [f"Tool created: {data['path']}"]
            if data.get("hot_loaded"):
                lines.append("Hot-loaded into the global catalog - every agent can discover it immediately.")
            else:
                lines.append("Restart north to activate (hot-load failed - check implementation).")
            return "\n".join(lines)

        if action == "update":
            lines = [f"Tool updated: {data['path']}"]
            if data.get("hot_loaded"):
                lines.append("Hot-loaded - changes active immediately via delegate_task or next message.")
            else:
                lines.append("Restart north to apply changes (hot-load failed - check implementation).")
            return "\n".join(lines)

        return str(data)

    async def run(self, input: ToolInput) -> ToolOutput:
        action = (input.params.get("action") or "create").strip()

        if action == "list":
            return _list_tools(self._tools_dir)
        if action == "read":
            return _read_tool(input.params.get("name") or "", self._tools_dir)
        if action in ("create", "update"):
            # Fail closed: model-authored code may be hot-loaded into the live
            # process, so it must never land without a human looking at it.
            refused = await self._gate(input.params, action)
            if refused is not None:
                return refused
            return self._create(input.params) if action == "create" else self._update(input.params)

        return ToolOutput(success=False, error=f"Unknown action '{action}'. Use: list, read, create, update.")

    async def _gate(self, params: dict, action: str) -> ToolOutput | None:
        """Show the proposed code and wait. ``None`` when it may be written."""
        name = params.get("name", "unknown")
        tool_type = params.get("tool_type", "specialized")
        content = (params.get("content") or "").strip()
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
            declined="Tool creation cancelled by user.",
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

        target_dir = self._tools_dir / tool_type
        target_dir.mkdir(parents=True, exist_ok=True)

        file_path = target_dir / f"{tool_name}.py"
        if file_path.exists():
            return ToolOutput(
                success=False,
                error=(
                    f"Tool '{tool_name}' already exists at {file_path}. "
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

        hot_loaded = self._hot_load(file_path, tool_type)

        return ToolOutput(
            success=True,
            data={
                "action": "create",
                "path": str(file_path),
                "hot_loaded": hot_loaded,
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

        path = _find_tool_path(tool_name, self._tools_dir)
        if path is None:
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

        tool_type = "universal" if (path.parent.name == "universal") else "specialized"
        mutation = None
        if self._self_edit_policy is not None:
            try:
                mutation = self._self_edit_policy.begin(path, "update")
            except PermissionError as exc:
                return ToolOutput(success=False, error=str(exc))
        path.write_text(content, encoding="utf-8")
        if mutation is not None:
            self._self_edit_policy.commit(mutation)

        hot_loaded = self._hot_load(path, tool_type)

        return ToolOutput(
            success=True,
            data={
                "action": "update",
                "path": str(path),
                "hot_loaded": hot_loaded,
            },
        )

    # ── Hot-loading ───────────────────────────────────────────────────────────

    def _hot_load(self, path: Path, tool_type: str) -> bool:
        """Dynamically import the tool and register it in the running registry.

        Returns True if the tool was successfully loaded and registered.
        """
        if self._registry is None:
            return False

        package = f"tools.{tool_type}"
        module_name = f"{package}.{path.stem}"

        # Evict stale module so importlib picks up the fresh file.
        sys.modules.pop(module_name, None)

        try:
            module = importlib.import_module(module_name)
        except Exception:
            return False

        for obj in vars(module).values():
            if isinstance(obj, type) and issubclass(obj, Tool) and obj is not Tool and not inspect.isabstract(obj):
                try:
                    instance = obj()
                    self._registry.register(instance)
                    return True
                except Exception:
                    continue

        return False


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
    rows = []
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
                rows.append(
                    {
                        "name": name_m.group(1) if name_m else path.stem,
                        "type": kind,
                        "source": source,
                        "description": desc_m.group(1) if desc_m else "(no description)",
                        "path": str(path),
                    }
                )
    return ToolOutput(success=True, data={"action": "list", "tools": rows})


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

def _find_tool_path(tool_name: str, learned_root: Path = _TOOLS_ROOT) -> Path | None:
    for root in (learned_root, _TOOLS_ROOT):
        for kind in ("universal", "specialized"):
            p = root / kind / f"{tool_name}.py"
            if p.exists():
                return p
    return None


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
