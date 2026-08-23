"""Claims-vs-evidence verification (orchestrator stage).

Agents narrate what they did ("created the file", "tests pass"). The model has
no idea whether that is true - it writes what a successful answer sounds like.
This module cross-checks such claims in an agent's final answer against the
tools that actually *succeeded*, so a fabricated "I ran the tests and they pass"
with no test execution is flagged rather than recorded as a clean completion.

Conservative by design: the patterns favour precision over recall - better to
miss a borderline claim than to cry wolf on a legitimate one. See
docs/CODING_STYLE.md Section 16.1.2.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

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

# Deterministic physical check: regex matching explicit file path claims like "saved to /path/to/file.md"
_EXPLICIT_PATH_CLAIM_RE = re.compile(
    r"\b(?:saved|written|created|stored|compiled|exported|generated)\s+(?:to\s+|at\s+|in\s+)?[`'\"]?([~/\.\w\-\_]+/[~\w\.\-\_]+\.[a-zA-Z0-9]+)[`'\"]?",
    re.IGNORECASE,
)

# (label, claim pattern, tools whose successful use substantiates the claim).
_RULES: tuple[tuple[str, re.Pattern[str], frozenset[str]], ...] = (
    (
        "creating or editing a file or briefing",
        re.compile(
            r"\b(?:creat(?:e|ed|ing)|wr(?:o|i)te|rewr(?:o|i)te|add(?:ed)?|sav(?:e|ed)|"
            r"generat(?:e|ed)|updat(?:e|ed)|modif(?:y|ied)|edit(?:ed)?|compil(?:e|ed|ing)|"
            r"assembl(?:e|ed|ing)|produc(?:e|ed|ing)|prepar(?:e|ed|ing)|buil(?:t|d|ding)|"
            r"implement(?:s|ed|ing)?|refactor(?:s|ed|ing)?|fix(?:es|ed|ing)?)\b"
            r"[^.\n]{0,60}"
            r"\b(?:file|script|module|unit\s+test|test\s+file|test_\w+|briefing|digest|report|summary|document|doc|changelog|"
            r"\w+\.(?:py|ts|js|tsx|go|rs|java|md|json|txt|ya?ml|sh|sql))\b"
            r"|\b(?:briefing|digest|report|summary|file|script|document|doc)\s+(?:is\s+|was\s+)?(?:saved|written|compiled|generated|created|produced|stored|built)\b",
            re.IGNORECASE,
        ),
        frozenset({"write_file", "patch_file", "create_tool"}),
    ),
    (
        "running a check, test, or verification",
        re.compile(
            r"\btests?\s+(?:are\s+|now\s+)?(?:pass(?:ed|ing|es)?|green|succeed(?:ed|ing)?)\b"
            r"|\ball\s+tests?\s+pass"
            r"|\b(?:ran|executed|i\s+tested)\b[^.\n]{0,40}"
            r"\b(?:test|tests|suite|pytest|unittest|npm|build|the\s+script|the\s+command)\b"
            r"|\b(?:type[-\s]?check(?:s|ed|ing)?|types?)\s+(?:pass(?:ed|es)?|are\s+clean|clean)\b"
            r"|\bno\s+type\s+errors?\b"
            r"|\bi\s+verified\b|\bverified\s+(?:that|the|it|by)\b",
            re.IGNORECASE,
        ),
        frozenset({"bash", "check_types"}),
    ),
    (
        "committing or pushing changes",
        re.compile(
            r"\b(?:committed|pushed|merged)\b|\bopened?\s+a\s+(?:pr|pull\s+request)\b",
            re.IGNORECASE,
        ),
        frozenset({"git", "gh"}),
    ),
    (
        "citing external information",
        re.compile(
            r"\baccording to\b"
            r"|\b(?:studies|research|reports?|data|surveys?)\s+(?:show|shows|indicate[sd]?|found|suggest[s]?)\b"
            r"|\bas\s+reported\s+by\b"
            r"|\bsources?\s*:"
            r"|\bper\s+the\s+(?:latest|official)\b",
            re.IGNORECASE,
        ),
        frozenset({"web_search", "fetch_url"}),
    ),
)


def _verify_path_existence(output: str, workspace: str | None = None) -> list[str]:
    """Check explicit path claims against physical filesystem reality."""
    violations: list[str] = []
    ws_path = Path(workspace).expanduser().resolve() if workspace else None
    for match in _EXPLICIT_PATH_CLAIM_RE.finditer(output):
        window = output[max(0, match.start() - _GOVERNING_WINDOW_CHARS) : match.start()]
        if _NON_COMPLETION_RE.search(window):
            continue
        raw_path = match.group(1)
        try:
            p = Path(raw_path).expanduser()
            if not p.is_absolute() and ws_path:
                p = (ws_path / p).resolve()
            if not p.exists():
                violations.append(f"output claims file was saved to '{raw_path}' but no such file exists on disk")
        except Exception:
            pass
    return violations


def _has_completion_claim(output: str, pattern: re.Pattern[str]) -> bool:
    """True if *output* asserts the claim as a completed action.

    A match governed by an intent/hypothetical marker (a plan, a suggestion, a
    past-tense reflection) in the preceding window does not count - only an
    unqualified assertion that the action was done.
    """
    for m in pattern.finditer(output):
        window = output[max(0, m.start() - _GOVERNING_WINDOW_CHARS) : m.start()]
        if not _NON_COMPLETION_RE.search(window):
            return True
    return False


def verify_claims(
    output: str,
    successful_tools: Iterable[str],
    workspace: str | None = None,
) -> list[str]:
    """Return violations: claims in *output* unsupported by tool evidence or physical reality.

    Each violation is a human-readable sentence. An empty list means nothing was
    flagged (either no actionable claims, or every claim has matching evidence).
    """
    if not output:
        return []
    succeeded = set(successful_tools)
    violations: list[str] = []

    # 1. Deterministic physical path check: verify claimed output files exist on disk
    for path_violation in _verify_path_existence(output, workspace=workspace):
        if path_violation not in violations:
            violations.append(path_violation)

    # 2. Evidence gate checks against recorded tool executions
    for label, pattern, required in _RULES:
        if not (required & succeeded) and _has_completion_claim(output, pattern):
            tool_list = " or ".join(f"`{t}`" for t in sorted(required))
            msg = f"output describes {label} but no successful {tool_list} call was recorded"
            if msg not in violations:
                violations.append(msg)

    return violations
