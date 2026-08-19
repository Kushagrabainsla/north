"""Shared text utilities."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_FENCE_OPEN_RE = re.compile(r"^```[\w-]*\s*\n")
_FENCE_CLOSE_RE = re.compile(r"\n?```\s*$")

# Em dash (—) and horizontal bar (―) read as an "AI tell" in prose; collapse the
# surrounding *inline* whitespace (not newlines) so ``a—b`` and ``a — b`` both
# become ``a - b``. En dash (–) is replaced in place below, preserving spacing.
_EM_DASH_RE = re.compile(r"[^\S\n]*[\u2014\u2015][^\S\n]*")
_EN_DASH = "\u2013"

# Suffixes whose bytes may be code or test-verified data: never rewrite a dash in
# these (a mutated string literal or data value could change behaviour or break a
# test the author already ran). Prose/doc files and unknown types are normalized.
_CODE_DATA_SUFFIXES: frozenset[str] = frozenset(
    {
        ".py", ".pyi", ".ipynb", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
        ".go", ".rs", ".java", ".kt", ".kts", ".scala", ".c", ".h", ".cpp", ".hpp",
        ".cc", ".cs", ".rb", ".php", ".swift", ".m", ".mm", ".sh", ".bash", ".zsh",
        ".ps1", ".sql", ".r", ".jl", ".lua", ".pl", ".dart", ".ex", ".exs",
        ".json", ".jsonl", ".ndjson", ".csv", ".tsv", ".parquet",
        ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".env", ".properties",
        ".xml", ".proto", ".lock", ".gradle", ".tf", ".bat",
    }
)


def normalize_dashes(text: str) -> str:
    """Replace em/en dashes with plain punctuation (north never writes the "AI tell" —).

    Em dashes and horizontal bars become a spaced hyphen (``" - "``) with surrounding
    inline whitespace collapsed, so ``a—b`` and ``a — b`` both render as ``a - b``.
    En dashes become a plain hyphen in place, preserving spacing (``3–5`` -> ``3-5``,
    ``a – b`` -> ``a - b``). Hyphen-minus (``-``) is untouched, so markdown ``---``
    frontmatter, ``-`` bullet lists, and existing hyphens are unaffected; newlines are
    never crossed, so line and list structure is preserved.
    """
    if not text:
        return text
    text = _EM_DASH_RE.sub(" - ", text)
    return text.replace(_EN_DASH, "-")


def should_normalize_prose(path: str | Path) -> bool:
    """True when a file path is prose/doc (dashes should be normalized) not code/data."""
    return Path(path).suffix.lower() not in _CODE_DATA_SUFFIXES


def strip_code_fences(text: str) -> str:
    """Strip a wrapping ``` fence (with optional language tag) from LLM output.

    Models asked for JSON frequently wrap it in a fenced code block anyway;
    every JSON-parsing call site shares this one normalization.
    """
    cleaned = text.strip()
    if not cleaned.startswith("```"):
        return cleaned
    cleaned = _FENCE_OPEN_RE.sub("", cleaned)
    cleaned = _FENCE_CLOSE_RE.sub("", cleaned)
    return cleaned.strip()


def extract_json(text: str) -> Any:
    """Leniently extract a JSON value from arbitrary model output.

    Models that can't honour ``response_format`` (many free-tier models) return
    the JSON as plain text - possibly wrapped in prose, fenced, or with a
    trailing comma. Rather than hard-failing, find the first balanced
    ``{...}`` or ``[...]`` span and parse that. Mirrors how Hermes tolerates
    non-API-enforced JSON so free models can serve structured requests.

    Raises ValueError if no balanced JSON span is found.
    """
    if text is None:
        raise ValueError("no text to parse")
    candidate = strip_code_fences(text).strip()
    # Fast path: the whole thing is JSON.
    try:
        return json.loads(candidate)
    except (ValueError, TypeError):
        pass
    # Scan for the first balanced object/array. Free models sometimes emit a
    # leading sentence ("Here is the plan:") before the JSON.
    for opener, closer in (("{", "}"), ("[", "]")):
        start = candidate.find(opener)
        while start != -1:
            depth = 0
            in_str = False
            esc = False
            end = -1
            for i in range(start, len(candidate)):
                ch = candidate[i]
                if in_str:
                    if esc:
                        esc = False
                    elif ch == "\\":
                        esc = True
                    elif ch == '"':
                        in_str = False
                    continue
                if ch == '"':
                    in_str = True
                elif ch == opener:
                    depth += 1
                elif ch == closer:
                    depth -= 1
                    if depth == 0:
                        end = i
                        break
            if end != -1:
                span = candidate[start : end + 1]
                try:
                    return json.loads(span)
                except (ValueError, TypeError):
                    pass
            start = candidate.find(opener, start + 1)
    raise ValueError("no JSON object/array found in model output")


STOPWORDS: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "the",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "could",
        "should",
        "may",
        "might",
        "shall",
        "can",
        "to",
        "of",
        "in",
        "for",
        "on",
        "with",
        "at",
        "by",
        "from",
        "and",
        "or",
        "but",
        "not",
        "i",
        "my",
        "me",
        "we",
        "our",
        "you",
        "your",
        "it",
        "its",
    }
)


def strip_html(html: str) -> str:
    """Extract readable text from HTML using BeautifulSoup."""
    from bs4 import BeautifulSoup  # type: ignore[import-untyped]

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
        tag.decompose()
    return " ".join(soup.get_text(separator="\n").split())
