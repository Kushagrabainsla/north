"""Locating the text an edit means to replace, and saying something useful when it isn't there.

Exact substring matching is right, and it is also brittle in one specific way: a
model reproducing a block from memory gets the characters right and the leading
whitespace wrong. The edit then fails with "check exact whitespace and newlines"
- which does not say what the whitespace actually *is*, so the retry is another
guess, and the retry costs a full round-trip on a model generating at 21 tokens
a second.

Two things fix most of it. Match on the lines' content when the exact bytes miss,
re-indenting the replacement to the file's own indentation so the result is still
correct. And when nothing matches, show the closest text in the file with line
numbers, so the next attempt is informed rather than another guess.

Uniqueness is never traded away. A tolerant match is accepted only when it is the
only one, because silently editing the wrong of two similar blocks is far worse
than failing.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass

# How much of the file to show around the closest match. Enough to re-anchor an
# edit; short enough not to flood the model's context on every miss.
_CONTEXT_LINES = 3


@dataclass(frozen=True, slots=True)
class Match:
    """Where an edit lands, and how it was found."""

    start: int
    end: int
    #: "exact" or "reindented" - a reindented match had its replacement shifted
    #: to the file's own indentation.
    how: str


def _line_starts(content: str) -> list[int]:
    starts, offset = [0], 0
    for line in content.split("\n")[:-1]:
        offset += len(line) + 1
        starts.append(offset)
    return starts


def _indent_of(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def reindent(replacement: str, from_indent: str, to_indent: str) -> str:
    """Shift *replacement* from the model's indentation to the file's.

    A tolerant match means the model wrote the block at one indentation and the
    file has it at another; pasting the replacement unchanged would leave the
    file syntactically wrong, which is a worse outcome than the failed edit this
    is meant to rescue.
    """
    if from_indent == to_indent:
        return replacement
    out: list[str] = []
    for line in replacement.split("\n"):
        if not line.strip():
            out.append(line)  # blank lines carry no indentation to shift
        elif not from_indent:
            # Prepend, never re-flatten: the line's own indentation is its
            # structure, and stripping it turns a nested block into a flat one.
            out.append(to_indent + line)
        elif line.startswith(from_indent):
            out.append(to_indent + line[len(from_indent) :])
        else:
            out.append(line)  # not shaped as expected - leave it rather than mangle it
    return "\n".join(out)


def _exact_occurrences(content: str, needle: str) -> list[int]:
    found, start = [], content.find(needle)
    while start != -1:
        found.append(start)
        start = content.find(needle, start + 1)
    return found


def _tolerant_spans(content: str, needle: str) -> list[tuple[int, int, str, str]]:
    """Windows whose lines match *needle*'s ignoring leading whitespace.

    Returns ``(start, end, file_indent, needle_indent)`` per match.
    """
    trailing_newline = needle.endswith("\n")
    body = needle[:-1] if trailing_newline else needle
    needle_lines = body.split("\n")
    if not needle_lines or not body.strip():
        return []
    wanted = [line.strip() for line in needle_lines]

    content_lines = content.split("\n")
    starts = _line_starts(content)
    span_count = len(needle_lines)
    spans: list[tuple[int, int, str, str]] = []
    for i in range(len(content_lines) - span_count + 1):
        window = content_lines[i : i + span_count]
        if [line.strip() for line in window] != wanted:
            continue
        start = starts[i]
        end = starts[i + span_count - 1] + len(content_lines[i + span_count - 1])
        if trailing_newline and end < len(content):
            end += 1
        spans.append((start, end, _indent_of(window[0]), _indent_of(needle_lines[0])))
    return spans


def _numbered(content: str, first: int, last: int) -> str:
    lines = content.split("\n")
    lo, hi = max(0, first), min(len(lines), last)
    width = len(str(hi))
    return "\n".join(f"{i + 1:>{width}} | {lines[i]}" for i in range(lo, hi))


def _closest_region(content: str, needle: str) -> str:
    """The part of the file that most resembles *needle*, with line numbers.

    "not found" on its own tells the model nothing it did not already know. What
    is actually in the file, at the place it meant, is what lets the next attempt
    be exact.
    """
    content_lines = content.split("\n")
    needle_lines = [line for line in needle.split("\n") if line.strip()]
    if not needle_lines or not content_lines:
        return ""
    anchor = needle_lines[0].strip()
    matcher = difflib.SequenceMatcher(a=anchor)
    best_index, best_ratio = -1, 0.0
    for index, line in enumerate(content_lines):
        matcher.set_seq2(line.strip())
        ratio = matcher.quick_ratio()
        if ratio > best_ratio:
            best_index, best_ratio = index, ratio
    if best_index < 0 or best_ratio < 0.5:
        return ""
    first = best_index - _CONTEXT_LINES
    last = best_index + len(needle_lines) + _CONTEXT_LINES
    return _numbered(content, first, last)


def find_unique(content: str, needle: str) -> tuple[Match | None, str]:
    """Locate *needle* in *content*. Returns ``(match, error)``; one is always empty.

    Exact first. Only if that finds nothing is the indentation-tolerant pass
    tried, and only a single tolerant match is accepted - two candidates means
    the edit is ambiguous, and guessing between them can silently corrupt a file.
    """
    exact = _exact_occurrences(content, needle)
    if len(exact) == 1:
        return Match(exact[0], exact[0] + len(needle), "exact"), ""
    if len(exact) > 1:
        lines = sorted({content.count("\n", 0, offset) + 1 for offset in exact})
        shown = ", ".join(str(line) for line in lines[:8])
        return None, (
            f"old_string appears {len(exact)} times (lines {shown}). "
            "Include more surrounding lines so it matches exactly one place."
        )

    tolerant = _tolerant_spans(content, needle)
    if len(tolerant) == 1:
        start, end, _file_indent, _needle_indent = tolerant[0]
        return Match(start, end, "reindented"), ""
    if len(tolerant) > 1:
        lines = sorted({content.count("\n", 0, start) + 1 for start, _e, _f, _n in tolerant})
        shown = ", ".join(str(line) for line in lines[:8])
        return None, (
            f"old_string matches {len(tolerant)} places when indentation is ignored (lines {shown}). "
            "Include more surrounding lines so it matches exactly one place."
        )

    region = _closest_region(content, needle)
    hint = f"\nThe closest text in the file is:\n{region}" if region else ""
    return None, (
        "old_string was not found, even ignoring indentation. "
        "Copy the text exactly as it appears in the file - read the file first if you have not."
        f"{hint}"
    )


def indents_for(content: str, needle: str, match: Match) -> tuple[str, str]:
    """``(needle_indent, file_indent)`` for a reindented match; empty for exact."""
    if match.how != "reindented":
        return "", ""
    for start, _end, file_indent, needle_indent in _tolerant_spans(content, needle):
        if start == match.start:
            return needle_indent, file_indent
    return "", ""
