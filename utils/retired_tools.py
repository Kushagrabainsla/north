"""Tools north removed, and the tool that does their work now.

Issue #78 removed the file tools: the shell reads, lists and searches files.
A skill saved before then still names them, and a flow that runs it was tested
and activated with those names in its fingerprint. Editing the saved skill
would change that fingerprint and stop the flow until it is tested again, and
leaving the old names unresolved would stop it as "unknown tool". So a retired
name resolves to its replacement wherever a skill's tools are resolved, and the
saved skill and its flow stay as they were. A new skill names the current tool.
"""

from __future__ import annotations

RETIRED_TOOLS: dict[str, str] = dict.fromkeys(
    (
        "read_file",
        "list_dir",
        "glob",
        "search_files",
        "search_code",
        "search_symbols",
        "find_references",
        "goto_definition",
        # Removed before issue #78; text still told the model to use it.
        "check_types",
    ),
    "bash",
)


def current_tool(name: str) -> str:
    """The tool that does *name*'s work now: its replacement if it was retired, else *name* itself."""
    return RETIRED_TOOLS.get(name, name)
