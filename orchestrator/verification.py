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

# Words that frame a claim verb as an intention, plan, or hypothetical rather
# than a completed action: "I should create the file", "let's write a test",
# "we need to generate the spec", "the files I created earlier were too brief".
# When one of these governs the claim verb, the sentence is not a completion
# claim, so it must not be checked against tool evidence. Reflective past-tense
# narration ("the file I created") is caught by the relative-pronoun markers.
_NON_COMPLETION_RE = re.compile(
    r"\b(?:should|would|could|will|shall|can|might|may|let'?s|going\s+to|gonna|"
    r"plan(?:ning)?\s+to|need(?:s|ed)?\s+to|want(?:s|ed)?\s+to|try(?:ing)?\s+to|"
    r"intend(?:s|ed)?\s+to|about\s+to|hope\s+to|aim\s+to|propose\s+to|"
    r"(?:file|files|script|test|tests|spec|code)\s+(?:i|we|you))\b",
    re.IGNORECASE,
)

# How far back to look for a non-completion marker governing a claim verb.
_GOVERNING_WINDOW_CHARS = 40

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


def _has_completion_claim(output: str, pattern: re.Pattern[str]) -> bool:
    """True if *output* asserts the claim as a completed action.

    A match governed by an intent/hypothetical marker (a plan, a suggestion, a
    past-tense reflection) in the preceding window does not count — only an
    unqualified assertion that the action was done.
    """
    for m in pattern.finditer(output):
        window = output[max(0, m.start() - _GOVERNING_WINDOW_CHARS) : m.start()]
        if not _NON_COMPLETION_RE.search(window):
            return True
    return False


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
        if not (required & succeeded) and _has_completion_claim(output, pattern):
            tool_list = " or ".join(f"`{t}`" for t in sorted(required))
            violations.append(f"output describes {label} but no successful {tool_list} call was recorded")
    return violations
