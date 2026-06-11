"""Shared text utilities."""

from __future__ import annotations

import re

_FENCE_OPEN_RE = re.compile(r"^```[\w-]*\s*\n")
_FENCE_CLOSE_RE = re.compile(r"\n?```\s*$")


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
