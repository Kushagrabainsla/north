"""One approval layer: nothing outside `approval/` decides whether north may act.

CODING_STYLE §7.3. Each check lists the modules allowed to do the thing, with the
reason. A new entry needs a reason too; a "until step N" entry is a staged
migration (ADR 0003) and is deleted when that step lands.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Iterator
from pathlib import Path

ROOT = Path(__file__).parents[3]
_SKIPPED = ("tests", "experiments", ".venv", "web/node_modules", "build")

# Modules outside approval/ and config/ that may name the approval mode.
MODE_READERS = {
    "orchestrator/api/settings.py": "the one API every surface shows and sets the mode through",
    "tools/specialized/north_config.py": "sets the mode when asked in chat",
    "agents/agentic_llm_agent.py": "until step 7: the memory decider answers questions in autonomous",
    "orchestrator/orchestrator.py": "until step 7: the memory decider replaces _human_available",
}
# Modules outside approval/ that may build the card channel.
INTERACTION_BUILDERS = {
    "orchestrator/app.py": "the composition root builds the one instance",
}
# Modules that may call Tool.run directly instead of Tool.execute.
RAW_TOOL_RUNS = {
    "web/extensions.py": "the user is acting from the dashboard: there is nobody else to ask",
    "tools/universal/create_tool.py": "the candidate's test run was itself approved",
}


def _sources() -> Iterator[tuple[str, ast.Module]]:
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith(_SKIPPED) or "/.venv/" in rel:
            continue
        yield rel, ast.parse(path.read_text(encoding="utf-8"), filename=rel)


def _modules_where(found: Callable[[ast.AST], bool], *, outside: tuple[str, ...]) -> set[str]:
    return {
        rel for rel, tree in _sources() if not rel.startswith(outside) and any(found(node) for node in ast.walk(tree))
    }


def _names_the_mode(node: ast.AST) -> bool:
    if isinstance(node, ast.ImportFrom):
        return node.module == "config.approval_mode"
    return isinstance(node, ast.Name) and node.id in {"ApprovalMode", "resolve_approval_mode"}


def _builds_interaction(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "UserInteraction"


def _runs_a_tool_raw(node: ast.AST) -> bool:
    """`<tool>.run(ToolInput(...))` - a tool acting without the approval layer."""
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "run"):
        return False
    receiver = ast.unparse(node.func.value).lower()
    argument = ast.unparse(node.args[0]).lower() if node.args else ""
    return "tool" in receiver or "toolinput" in argument or "tool_input" in argument


def test_only_the_approval_layer_reads_the_mode() -> None:
    assert _modules_where(_names_the_mode, outside=("approval/", "config/")) == set(MODE_READERS)


def test_only_the_approval_layer_builds_the_card_channel() -> None:
    assert _modules_where(_builds_interaction, outside=("approval/",)) == set(INTERACTION_BUILDERS)


def test_tools_act_only_through_execute() -> None:
    assert _modules_where(_runs_a_tool_raw, outside=("tools/base.py",)) == set(RAW_TOOL_RUNS)
