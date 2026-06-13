"""Tests for the /transcribe body-size cap (review finding R3#18)."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from orchestrator.api_router import MAX_TRANSCRIBE_BYTES, _read_body_capped


class FakeRequest:
    def __init__(self, chunks: list[bytes], content_length: str | None = None) -> None:
        self._chunks = chunks
        self.headers = {"content-length": content_length} if content_length else {}

    async def stream(self):
        for chunk in self._chunks:
            yield chunk


async def test_small_body_passes() -> None:
    body = await _read_body_capped(FakeRequest([b"abc", b"def"]), max_bytes=100)
    assert body == b"abcdef"


async def test_declared_oversize_rejected_before_reading() -> None:
    request = FakeRequest([b"x"], content_length=str(MAX_TRANSCRIBE_BYTES + 1))
    with pytest.raises(HTTPException) as exc:
        await _read_body_capped(request, max_bytes=MAX_TRANSCRIBE_BYTES)
    assert exc.value.status_code == 413


async def test_streamed_oversize_rejected_mid_read() -> None:
    """A liar client (no/short Content-Length) is still cut off at the cap."""
    request = FakeRequest([b"x" * 60, b"x" * 60])
    with pytest.raises(HTTPException) as exc:
        await _read_body_capped(request, max_bytes=100)
    assert exc.value.status_code == 413


async def test_exact_cap_is_allowed() -> None:
    body = await _read_body_capped(FakeRequest([b"x" * 100]), max_bytes=100)
    assert len(body) == 100
