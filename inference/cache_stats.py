"""Conservative prompt-cache miss accounting.

Providers often cache automatically, but a cache miss is silent: the same prompt
prefix is billed again and the response merely reports zero cached tokens. This
module calls it a *likely* miss only after that provider has already reported
cache activity in the same run. That avoids falsely blaming providers which do
not expose cache counters at all.
"""

from __future__ import annotations

from dataclasses import dataclass

# Tiny changes at a cache breakpoint are normal provider granularity, not waste.
NOISE_FLOOR_TOKENS = 1_024


@dataclass(frozen=True, slots=True)
class CacheWasteTotals:
    """Likely cache misses across one agent run."""

    missed_tokens: int = 0
    miss_count: int = 0


class CacheWasteTracker:
    """Compare each prompt with the previous turn without over-claiming.

    ``tokens_in`` is the full prompt size a provider reported. On a warm turn,
    ``cached_tokens`` is the reused part. After any cache read/write is observed,
    a later turn that re-sends most of the prior prompt but does not read it from
    cache is counted as a likely miss. The first request has no prior prompt and
    is never counted.
    """

    def __init__(self) -> None:
        self._previous_prompt_tokens: int | None = None
        self._reported_cache = False
        self._missed_tokens = 0
        self._miss_count = 0

    def add(self, *, tokens_in: int, cached_tokens: int, cache_write_tokens: int) -> None:
        previous = self._previous_prompt_tokens
        reported_before = self._reported_cache
        if cached_tokens > 0 or cache_write_tokens > 0:
            self._reported_cache = True

        if previous is not None and reported_before and tokens_in > 0:
            # Only the part of today's prompt that also existed last turn could
            # have been reused. New tool output is not a cache miss.
            missed = min(previous, tokens_in) - cached_tokens
            if missed > NOISE_FLOOR_TOKENS:
                self._missed_tokens += missed
                self._miss_count += 1

        self._previous_prompt_tokens = tokens_in if tokens_in > 0 else previous

    @property
    def totals(self) -> CacheWasteTotals:
        return CacheWasteTotals(self._missed_tokens, self._miss_count)
