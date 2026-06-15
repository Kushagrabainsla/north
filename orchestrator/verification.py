"""Claims-vs-evidence verification (orchestrator stage).

Agents narrate what they did ("created the file", "tests pass"). The model has
no idea whether that is true — it writes what a successful answer sounds like.
This module cross-checks such claims in an agent's final answer against the
tools that actually *succeeded*, so a fabricated "I ran the tests and they pass"
with no test execution is flagged rather than recorded as a clean completion.

Conservative by design: the patterns favour precision over recall — better to
miss a borderline claim than to cry wolf on a legitimate one. See
docs/CODING_STYLE.md Section 16.1.2.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

# (label, claim pattern, tools whose successful use substantiates the claim).
_RULES: tuple[tuple[str, re.Pattern[str], frozenset[str]], ...] = (
    (
        "creating or editing a file",
        re.compile(
            r"\b(?:creat(?:e|ed|ing)|wr(?:o|i)te|add(?:ed)?|sav(?:e|ed)|"
            r"generat(?:e|ed)|updat(?:e|ed)|modif(?:y|ied)|edit(?:ed)?)\b"
            r"[^.\n]{0,60}"
            r"\b(?:file|script|module|unit\s+test|test\s+file|test_\w+|"
            r"\w+\.(?:py|ts|js|tsx|go|rs|java|md|json|txt|ya?ml|sh|sql))\b",
            re.IGNORECASE,
        ),
        frozenset({"write_file", "patch_file", "create_tool"}),
    ),
    (
        "running a command or test",
        re.compile(
            r"\btests?\s+(?:are\s+|now\s+)?(?:pass(?:ed|ing|es)?|green|succeed(?:ed|ing)?)\b"
            r"|\ball\s+tests?\s+pass"
            r"|\b(?:ran|executed|i\s+tested)\b[^.\n]{0,40}"
            r"\b(?:test|tests|suite|pytest|unittest|npm|build|the\s+script|the\s+command)\b",
            re.IGNORECASE,
        ),
        frozenset({"bash"}),
    ),
    (
        "committing or pushing changes",
        re.compile(
            r"\b(?:committed|pushed|merged)\b|\bopened?\s+a\s+(?:pr|pull\s+request)\b",
            re.IGNORECASE,
        ),
        frozenset({"git", "gh"}),
    ),
)


def verify_claims(output: str, successful_tools: Iterable[str]) -> list[str]:
    """Return violations: claims in *output* unsupported by a successful tool call.

    Each violation is a human-readable sentence. An empty list means nothing was
    flagged (either no actionable claims, or every claim has matching evidence).
    """
    if not output:
        return []
    succeeded = set(successful_tools)
    violations: list[str] = []
    for label, pattern, required in _RULES:
        if pattern.search(output) and not (required & succeeded):
            tool_list = " or ".join(f"`{t}`" for t in sorted(required))
            violations.append(f"output describes {label} but no successful {tool_list} call was recorded")
    return violations
