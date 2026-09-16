"""Deterministically reduce tool output without hiding its useful middle.

Tool output is evidence, not ordinary prose.  Errors, source locations, symbol
declarations, and diff anchors often occur far from the beginning, so a plain
prefix is a particularly lossy way to fit it into a model context window.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class _Signal:
    start: int
    end: int
    score: int


_SIGNAL_PATTERNS: tuple[tuple[re.Pattern[str], int], ...] = (
    (re.compile(r"(?im)\b(?:fatal|panic|traceback|exception|error|failed|failure)\b"), 100),
    (re.compile(r"(?m)(?:^|\s)(?:[A-Za-z]:)?[^\s:]+\.[A-Za-z0-9_+-]{1,12}:\d+(?::\d+)?"), 90),
    (re.compile(r"(?m)^\s*(?:async\s+)?(?:def|class|function|interface|type|struct|enum)\s+[A-Za-z_]\w*"), 70),
    (re.compile(r"(?m)^\s*(?:@@|diff --git|---\s|\+\+\+\s)"), 60),
    (re.compile(r"(?im)\b(?:warning|warn|assert(?:ion)?|expected|actual|found|match)\b"), 40),
)


def _candidate_signals(text: str, *, window: int) -> list[_Signal]:
    """Return bounded, de-duplicated source ranges around high-signal matches."""
    candidates: list[_Signal] = []
    seen: set[tuple[int, int]] = set()
    for pattern, score in _SIGNAL_PATTERNS:
        for match in pattern.finditer(text):
            line_start = text.rfind("\n", 0, match.start()) + 1
            line_end = text.find("\n", match.end())
            if line_end < 0:
                line_end = len(text)
            if line_end - line_start > window:
                half = max(16, window // 2)
                line_start = max(line_start, match.start() - half)
                line_end = min(line_end, match.end() + half)
            key = (line_start, line_end)
            if key not in seen:
                seen.add(key)
                candidates.append(_Signal(line_start, line_end, score))
    return candidates


def salient_excerpt(text: str, limit: int) -> str:
    """Return at most *limit* chars, prioritising diagnostic and source evidence.

    The excerpt keeps a little beginning and end for orientation, then spends
    most of its budget on high-signal lines from anywhere in the output.  The
    full output remains available through north's overflow handle.
    """
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text

    marker = "\n…[omitted]…\n"
    marker_cost = len(marker) * 2
    usable = max(0, limit - marker_cost)
    if usable < 48:
        return text[:limit]

    head_size = max(16, usable // 5)
    tail_size = max(16, usable // 10)
    signal_budget = max(0, usable - head_size - tail_size)

    candidates = _candidate_signals(text, window=max(80, signal_budget // 2))
    selected: list[_Signal] = []
    remaining = signal_budget
    for candidate in sorted(candidates, key=lambda item: (-item.score, item.start)):
        if candidate.start < head_size or candidate.end > len(text) - tail_size:
            continue
        if any(candidate.start < kept.end and kept.start < candidate.end for kept in selected):
            continue
        length = candidate.end - candidate.start
        if length <= remaining:
            selected.append(candidate)
            remaining -= length

    if not selected:
        # With no known signal, a balanced head/tail is more useful than an
        # arbitrary middle slice while still making the omission explicit.
        head_size = usable * 2 // 3
        tail_size = usable - head_size

    signal_text = "\n".join(text[item.start : item.end] for item in sorted(selected, key=lambda item: item.start))
    parts = [text[:head_size], marker]
    if signal_text:
        parts.extend((signal_text, marker))
    parts.append(text[-tail_size:])
    return "".join(parts)[:limit]
