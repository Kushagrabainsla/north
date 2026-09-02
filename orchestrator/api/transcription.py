"""Audio transcription for the dictation key."""

from __future__ import annotations

from fastapi import HTTPException, Request
from pydantic import BaseModel

from inference.models import TranscriptionRequest
from orchestrator.api.deps import _get_inference_router, router


class TranscriptionOut(BaseModel):
    text: str
    model_used: str
    cost_usd: float


# 25 MB ≈ 25 minutes of 16-bit 16 kHz WAV - far more than a dictation clip.
# Bounding the read prevents a single request from exhausting memory and
# putting an unbounded payload in front of a paid transcription API.
MAX_TRANSCRIBE_BYTES = 25 * 1024 * 1024


async def _read_body_capped(request: Request, max_bytes: int) -> bytes:
    """Read the request body, rejecting payloads larger than *max_bytes* (413)."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > max_bytes:
        raise HTTPException(status_code=413, detail=f"Body exceeds {max_bytes} byte limit.")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > max_bytes:
            raise HTTPException(status_code=413, detail=f"Body exceeds {max_bytes} byte limit.")
    return bytes(body)


@router.post("/transcribe", response_model=TranscriptionOut)
async def transcribe_audio(request: Request) -> TranscriptionOut:
    """Transcribe raw audio bytes (WAV/MP3) via OpenRouter Whisper.

    The request body must be the raw audio file bytes (max 25 MB). The
    Content-Type header should be audio/wav or audio/mpeg.
    """
    audio_bytes = await _read_body_capped(request, MAX_TRANSCRIBE_BYTES)
    if not audio_bytes:
        raise HTTPException(status_code=422, detail="Empty audio body.")

    result = await _get_inference_router().transcribe(TranscriptionRequest(audio=audio_bytes, component="perception"))
    return TranscriptionOut(
        text=result.text,
        model_used=result.model_used,
        cost_usd=result.cost_usd,
    )


