"""Before and after for tools in their own environments: one checkout's own create_tool on the same drafts.

Usage (from any directory):
    uv run --project <checkout> python <this file> <checkout> <label>

Each case writes a tool with the checkout's own `create_tool` (with an approval layer that says yes, as
you would on the card), switches it on, and calls it once. A throwaway north home is used. The results
hold booleans and versions only.

Cases:
  new_package      a tool that needs a package north does not have (tabulate 0.9.0)
  other_version    a tool that needs another version of a package north has (packaging 23.2)
  north_files      a tool that reads north's own secret key file
  panel_can_read   a view reading the tool's `list` call, which changes nothing
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace

CHECKOUT = Path(sys.argv[1]).resolve()
LABEL = sys.argv[2]
HOME = Path(tempfile.mkdtemp(prefix="north-envs-"))
os.environ["NORTH_HOME"] = str(HOME)
sys.path.insert(0, str(CHECKOUT))

from config.settings import settings  # noqa: E402
from tools.models import ToolInput  # noqa: E402
from tools.registry import ToolRegistry  # noqa: E402
from tools.universal.create_tool import CreateToolTool  # noqa: E402
from views.document import reference_checks  # noqa: E402

settings.north_home = HOME
HERE = Path(__file__).parent


class _YesOnTheCard:
    """The approval layer, answering every card yes, as the user would for these tools."""

    async def decide(self, request, task_id=None):
        return SimpleNamespace(allowed=True)

    def completed(self, request, task_id=None):
        return None

    def refused_paths(self, paths, task_id=None):
        return None


def _tool(name: str, body: str, imports: str = "", packages: list[str] | None = None) -> str:
    block = f"# /// script\n# dependencies = {json.dumps(packages)}\n# ///\n" if packages else ""
    return (
        f"{block}{imports}\nfrom tools.base import Tool\nfrom tools.models import ToolInput, ToolOutput\n\n\n"
        f"class T(Tool):\n    name = {name!r}\n    description = 'A test tool.'\n"
        "    parameters_schema = {'type': 'object', 'properties': {'action': {'type': 'string'}, "
        "'path': {'type': 'string'}}}\n    is_mutating = True\n    read_only_when = {'action': ['list']}\n\n"
        f"    async def run(self, input: ToolInput) -> ToolOutput:\n{body}"
    )


CASES = {
    "new_package": _tool(
        "uses_tabulate",
        "        return ToolOutput(success=True, data={'version': tabulate.__version__})\n",
        "import tabulate",
        ["tabulate==0.9.0"],
    ),
    "other_version": _tool(
        "uses_packaging",
        "        return ToolOutput(success=True, data={'version': packaging.__version__})\n",
        "import packaging",
        ["packaging==23.2"],
    ),
    "north_files": _tool(
        "reads_north",
        "        try:\n            n = len(Path(input.params['path']).read_bytes())\n"
        "            return ToolOutput(success=True, data={'read': True})\n"
        "        except OSError:\n            return ToolOutput(success=True, data={'read': False})\n",
        "from pathlib import Path",
    ),
    "panel_can_read": _tool("lamp_list", "        return ToolOutput(success=True, data={'lamps': []})\n"),
}


async def case(name: str, content: str) -> dict:
    registry = ToolRegistry(auto_register=False, learned_dir=HOME / "learned" / "tools", approvals=_YesOnTheCard())
    creator = CreateToolTool(tool_registry=registry, tools_dir=HOME / "learned" / "tools")
    creator.approvals = _YesOnTheCard()
    tool_name = content.split("name = ")[1].split("\n")[0].strip("'\"")
    key = HOME / "secret.key"
    key.write_text("north-server-key-for-the-check", encoding="utf-8")
    test = {"action": "list"} if name != "north_files" else {"action": "read", "path": str(key)}
    call = {"action": "create", "name": tool_name, "content": content, "test_params": test}
    out = await creator.run(ToolInput(params=call))
    row: dict = {"case": name, "created": out.success, "why_not": (out.error or "")[:160] if not out.success else ""}
    if not out.success:
        return row
    tool = registry.get(tool_name)
    result = await tool.execute(ToolInput(params=test))
    row["result"] = result.data
    if name == "panel_can_read":
        tool_problem, _ = reference_checks(registry, None)
        row["view_may_read_list"] = tool_problem(tool_name, {"action": "list"}) is None
    if hasattr(tool, "aclose"):
        await tool.aclose()
    return row


async def main() -> None:
    rows = [await case(name, content) for name, content in CASES.items()]
    out = {"checkout": LABEL, "north_packaging_after": version("packaging"), "cases": rows}
    (HERE / "results").mkdir(exist_ok=True)
    (HERE / "results" / f"verify_{LABEL}.json").write_text(json.dumps(out, indent=1) + "\n", encoding="utf-8")
    print(json.dumps(out, indent=1))


asyncio.run(main())
