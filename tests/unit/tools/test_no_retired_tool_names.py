"""No prompt, built-in skill or tool description names a tool that no longer exists.

Section 16.3 of CODING_STYLE.md told agents to use `check_types` and a `shell` tool
for months after both were gone, and two tool descriptions still said "verify with
check_types". The model is told what its prompts say, so a dead name sends it to a
tool it cannot call (issue #78).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from utils.retired_tools import RETIRED_TOOLS, current_tool

ROOT = Path(__file__).resolve().parents[3]
# What the model reads: system prompts, built-in skills, and the tools' own code (descriptions and messages).
TEXTS = sorted(
    [
        *ROOT.glob("agents/*/prompts/*.md"),
        *ROOT.glob("resources/prompts/*.md"),
        *ROOT.glob("resources/builtin-skills/*/*.md"),
        *ROOT.glob("tools/universal/*.py"),
        *ROOT.glob("tools/specialized/*.py"),
    ]
)


def _mentions(text: str, name: str) -> bool:
    # "glob" is also an ordinary word ("a glob pattern"); as a tool it was always written `glob`.
    pattern = rf"`{name}`" if name == "glob" else rf"(?<![\w.-]){re.escape(name)}(?![\w-])"
    return re.search(pattern, text) is not None


@pytest.mark.parametrize("path", TEXTS, ids=lambda path: str(path.relative_to(ROOT)))
def test_names_no_retired_tool(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    named = sorted(name for name in RETIRED_TOOLS if _mentions(text, name))
    instead = sorted({current_tool(name) for name in named})
    assert not named, f"{path.relative_to(ROOT)} names retired tools {named}; use {instead}"


def test_a_saved_skill_that_names_a_retired_tool_still_resolves() -> None:
    """A flow tested with such a skill keeps running: the name resolves to the tool doing the work now."""
    from flows.validation import resolve_execution_tools
    from skills.models import SkillExecution

    execution = SkillExecution(agent="north", tools=("web_search", "list_dir", "read_file", "write_file"))

    assert resolve_execution_tools(execution, {}) == ("web_search", "bash", "write_file")
