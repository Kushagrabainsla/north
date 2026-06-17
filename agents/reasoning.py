"""Separate a model's private reasoning (chain-of-thought) from its answer.

Some models emit their private reasoning inline, wrapped in tags like
``<thought>...</thought>``. That text is not an answer and must never be shown
as output or stored as the result. This module provides two views of the same
rule set so the live stream and the persisted output agree exactly:

- :func:`strip_reasoning` - remove reasoning blocks from a complete string.
- :class:`ReasoningStreamSplitter` - the streaming equivalent: fed text chunks
  as they arrive, it classifies each fragment as ``answer`` or ``reasoning`` so a
  UI can route reasoning to a dimmed channel instead of the answer.

A half-open reasoning block (an open tag with no matching close) is discarded by
both - an unterminated thought is never an answer.

See docs/CODING_STYLE.md Section 15.
"""

from __future__ import annotations

import re

# Tag *names* whose <name>...</name> spans are private reasoning, not answer.
# Matched case-sensitively (models emit lowercase) so the streaming splitter and
# the static strip below classify identical bytes identically.
REASONING_TAGS: tuple[str, ...] = ("thought", "thinking", "reasoning")

_OPEN_MARKERS: tuple[str, ...] = tuple(f"<{t}>" for t in REASONING_TAGS)
_CLOSE_FOR: dict[str, str] = {f"<{t}>": f"</{t}>" for t in REASONING_TAGS}
_MAX_MARKER_LEN: int = max(len(m) for m in (*_OPEN_MARKERS, *_CLOSE_FOR.values()))

_ALTERNATION = "|".join(REASONING_TAGS)
_BLOCK_RE = re.compile(rf"<(?:{_ALTERNATION})>.*?</(?:{_ALTERNATION})>", re.DOTALL)
# An unterminated reasoning block (open tag, no close) runs to end-of-text.
_DANGLING_RE = re.compile(rf"<(?:{_ALTERNATION})>.*\Z", re.DOTALL)


def strip_reasoning(text: str) -> str:
    """Return *text* with every reasoning block removed.

    Well-formed ``<tag>...</tag>`` spans are dropped; a dangling open tag with no
    matching close is dropped through end-of-string, mirroring the streaming
    splitter which discards an unterminated reasoning block.
    """
    if not text or "<" not in text:
        return text
    cleaned = _BLOCK_RE.sub("", text)
    cleaned = _DANGLING_RE.sub("", cleaned)
    return cleaned


def _earliest_marker(buf: str, markers: tuple[str, ...]) -> tuple[int, str]:
    """Return (index, marker) of the earliest complete marker in *buf*.

    Returns ``(-1, "")`` when no complete marker is present.
    """
    best_idx = -1
    best_marker = ""
    for m in markers:
        i = buf.find(m)
        if i != -1 and (best_idx == -1 or i < best_idx):
            best_idx, best_marker = i, m
    return best_idx, best_marker


def _hold_index(buf: str, markers: tuple[str, ...]) -> int:
    """Smallest index *i* such that ``buf[i:]`` is a proper prefix of a marker.

    Text from *i* onward might be the start of a marker split across the chunk
    boundary, so it is withheld until more text arrives. Returns ``len(buf)``
    when nothing needs holding.
    """
    n = len(buf)
    start = max(0, n - (_MAX_MARKER_LEN - 1))
    for i in range(start, n):
        tail = buf[i:]
        for m in markers:
            if len(tail) < len(m) and m.startswith(tail):
                return i
    return n


class ReasoningStreamSplitter:
    """Stateful splitter that classifies streamed text as answer vs reasoning.

    Feed it text chunks via :meth:`feed`; it returns ``(channel, fragment)``
    pairs where *channel* is ``"answer"`` or ``"reasoning"``. Tags may straddle
    chunk boundaries - a fragment that could begin a marker is held back until
    the next chunk confirms or denies it. Call :meth:`flush` at end of stream to
    release any held text.
    """

    __slots__ = ("_buf", "_active_close")

    def __init__(self) -> None:
        self._buf: str = ""
        self._active_close: str | None = None  # None ⇒ currently in the answer

    def feed(self, text: str) -> list[tuple[str, str]]:
        self._buf += text
        out: list[tuple[str, str]] = []
        while self._buf:
            if self._active_close is None:
                idx, marker = _earliest_marker(self._buf, _OPEN_MARKERS)
                if idx == -1:
                    hold = _hold_index(self._buf, _OPEN_MARKERS)
                    if hold > 0:
                        out.append(("answer", self._buf[:hold]))
                    self._buf = self._buf[hold:]
                    break
                if idx > 0:
                    out.append(("answer", self._buf[:idx]))
                self._active_close = _CLOSE_FOR[marker]
                self._buf = self._buf[idx + len(marker) :]
            else:
                close = self._active_close
                idx = self._buf.find(close)
                if idx == -1:
                    hold = _hold_index(self._buf, (close,))
                    if hold > 0:
                        out.append(("reasoning", self._buf[:hold]))
                    self._buf = self._buf[hold:]
                    break
                if idx > 0:
                    out.append(("reasoning", self._buf[:idx]))
                self._active_close = None
                self._buf = self._buf[idx + len(close) :]
        return out

    def flush(self) -> list[tuple[str, str]]:
        """Release held text at end of stream.

        Held answer text is emitted - it was only withheld in case it began a
        marker that never arrived. A half-open reasoning block is discarded,
        matching :func:`strip_reasoning`.
        """
        out: list[tuple[str, str]] = []
        if self._buf and self._active_close is None:
            out.append(("answer", self._buf))
        self._buf = ""
        self._active_close = None
        return out
