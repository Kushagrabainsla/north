"""Overflow store for tool output that did not fit in the model's context.

north caps a single tool result at ``MAX_TOOL_RESULT_CHARS`` and shortens old
results when it compacts history. Both used to *discard* the part that did not
fit, and both kept the head - so a grep match halfway down a long file, or a
value in the body of a fetched page, was gone. The agent could not tell: a
truncated result looks like a complete one with a marker in it, so the model
reasons confidently from a hole.

Nothing is discarded now. The full text is kept here under a short handle, the
truncated copy carries that handle, and ``read_tool_output`` pages through or
searches the original. Truncation becomes "you were shown the first part",
which the agent can act on, instead of "this is all there was", which it
cannot.

Bounded on purpose: north is a long-running server, and an overflow store that
only grows is a leak. Oldest entries are evicted once either cap is hit, so a
handle can expire - callers must handle a miss (see ``read``).
"""

from __future__ import annotations

import re
import threading
from collections import OrderedDict
from dataclasses import dataclass

from utils.ids import generate_id

# How much overflow to keep, and for how many results. Both are needed: a few
# enormous outputs blow the byte cap while leaving the count untouched, and many
# small ones do the reverse.
_MAX_ENTRIES = 64
_MAX_TOTAL_CHARS = 32_000_000  # ~32 MB of text

# Default and ceiling for a single read, so paging through an overflow cannot
# reintroduce the context blowout that caused the spill in the first place.
DEFAULT_READ_CHARS = 8_000
MAX_READ_CHARS = 40_000

# Characters of surrounding text returned either side of a search hit.
_MATCH_CONTEXT_CHARS = 400
_MAX_MATCHES = 20

_HANDLE_PREFIX = "tool_output:"


@dataclass(frozen=True)
class SpillEntry:
    """One stored overflow: the full text plus what produced it."""

    handle: str
    tool_name: str
    text: str


@dataclass(frozen=True)
class SpillSlice:
    """A window into a stored overflow, with enough to ask for the next one."""

    handle: str
    tool_name: str
    text: str
    offset: int
    returned_chars: int
    total_chars: int

    @property
    def next_offset(self) -> int | None:
        """Where a following read should start, or None at the end of the text."""
        end = self.offset + self.returned_chars
        return end if end < self.total_chars else None


@dataclass(frozen=True)
class SpillMatch:
    """One search hit inside a stored overflow."""

    offset: int
    excerpt: str


class OutputSpillStore:
    """Bounded LRU of full tool outputs, addressed by handle.

    Guarded by a lock rather than assumed loop-local: results are capped on the
    event loop but a tool may read one from a worker thread, and a store whose
    safety depends on which thread happens to call it is a bug waiting for the
    first ``asyncio.to_thread``.
    """

    def __init__(self, max_entries: int = _MAX_ENTRIES, max_total_chars: int = _MAX_TOTAL_CHARS) -> None:
        self._entries: OrderedDict[str, SpillEntry] = OrderedDict()
        self._max_entries = max_entries
        self._max_total_chars = max_total_chars
        self._total_chars = 0
        self._lock = threading.Lock()

    def store(self, tool_name: str, text: str) -> str:
        """Keep *text* and return the handle that reads it back."""
        handle = f"{_HANDLE_PREFIX}{generate_id()}"
        with self._lock:
            self._entries[handle] = SpillEntry(handle=handle, tool_name=tool_name, text=text)
            self._total_chars += len(text)
            self._evict()
        return handle

    def _evict(self) -> None:
        """Drop oldest entries until both caps are satisfied. Caller holds the lock."""
        while self._entries and (len(self._entries) > self._max_entries or self._total_chars > self._max_total_chars):
            _, evicted = self._entries.popitem(last=False)
            self._total_chars -= len(evicted.text)

    def read(self, handle: str, offset: int = 0, limit: int = DEFAULT_READ_CHARS) -> SpillSlice | None:
        """Return a window of the stored text, or None when the handle is unknown.

        A miss is normal, not exceptional: handles expire under the caps above,
        and a task that sat idle can come back to one that has been evicted.
        """
        entry = self._touch(handle)
        if entry is None:
            return None
        total = len(entry.text)
        start = max(0, min(offset, total))
        window = max(1, min(limit, MAX_READ_CHARS))
        chunk = entry.text[start : start + window]
        return SpillSlice(
            handle=handle,
            tool_name=entry.tool_name,
            text=chunk,
            offset=start,
            returned_chars=len(chunk),
            total_chars=total,
        )

    def find(self, handle: str, pattern: str, max_matches: int = _MAX_MATCHES) -> list[SpillMatch] | None:
        """Search the stored text, returning excerpts around each hit.

        This is the point of keeping the overflow at all: the agent usually
        wants one specific thing out of a long output, and paging blindly
        through 60k characters to find it costs more context than the
        truncation saved. *pattern* is a regular expression; an invalid one
        raises ``re.error`` for the caller to report.
        """
        entry = self._touch(handle)
        if entry is None:
            return None
        compiled = re.compile(pattern, re.IGNORECASE | re.MULTILINE)
        matches: list[SpillMatch] = []
        for found in compiled.finditer(entry.text):
            start = max(0, found.start() - _MATCH_CONTEXT_CHARS)
            end = min(len(entry.text), found.end() + _MATCH_CONTEXT_CHARS)
            matches.append(SpillMatch(offset=found.start(), excerpt=entry.text[start:end]))
            if len(matches) >= max_matches:
                break
        return matches

    def _touch(self, handle: str) -> SpillEntry | None:
        """Look up *handle* and mark it most-recently-used."""
        with self._lock:
            entry = self._entries.get(handle)
            if entry is not None:
                self._entries.move_to_end(handle)
            return entry

    def clear(self) -> None:
        """Drop everything. Used by tests and by ``north reset``."""
        with self._lock:
            self._entries.clear()
            self._total_chars = 0


# One store per process: the agent loop writes overflow into it and the
# read_tool_output tool reads it back, and neither can be handed a reference by
# the other without threading it through every agent and tool constructor.
_store = OutputSpillStore()


def spill_store() -> OutputSpillStore:
    """The process-wide overflow store."""
    return _store


def store_overflow(tool_name: str, text: str) -> str:
    """Keep the full *text* of an oversized result; return its handle."""
    return _store.store(tool_name, text)


def carries_handle(text: str) -> bool:
    """Whether *text* is a result that has already been spilled and shrunk.

    Callers use this to avoid shrinking a shrunken result a second time, which
    would spill the truncated copy as a new entry and evict the original it
    points at.
    """
    return _HANDLE_PREFIX in text


def summary_note(handle: str, total_chars: int) -> str:
    """The marker left on a result that was replaced by a summary.

    Differs from `overflow_note` in what it can honestly promise. A truncated
    result shows the first few hundred characters and says nothing about the
    rest; a summary was written from the whole output, so the agent has been
    told what was in all of it. It still needs to know the summary is lossy and
    how to reach the original.
    """
    return (
        f"The {total_chars}-character output of this call was summarised above rather than kept in full. "
        f"The original is under handle '{handle}'. The summary was written from the whole output, but it "
        "is lossy - if you need an exact value it does not give you, call read_tool_output with this handle "
        "(action='find' with a regex to locate it, or action='read' with an offset to page on)."
    )


def overflow_note(handle: str, shown_chars: int, total_chars: int) -> str:
    """The marker left on a truncated result, phrased so the agent acts on it.

    Says what is missing, how much, and the exact call that gets it - a bare
    "[truncated]" told the model only that it had been given less, which it
    reliably ignored and then answered from the part it could see.
    """
    omitted = max(0, total_chars - shown_chars)
    seen = (
        f"The {total_chars}-character output of this tool call is no longer shown here"
        if shown_chars <= 0
        else f"Showing the first {shown_chars} of {total_chars} characters; {omitted} not shown"
    )
    return (
        f"{seen}. The full output is kept under handle '{handle}'. "
        "This is NOT the whole result - if what you need is not above, call read_tool_output with this handle "
        "(action='find' with a regex to locate it, or action='read' with an offset to page on). "
        "Do not answer as though the omitted part was empty."
    )
