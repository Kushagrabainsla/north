"""Pure helpers for the optional push-to-talk CLI command."""

from __future__ import annotations

import io
import wave
from typing import Any


def parse_hotkey(hotkey: str) -> frozenset[object]:
    """Return the pynput key objects named by a plus-separated hotkey."""
    from pynput import keyboard as kb

    def key_for(part: str) -> object:
        name = part.strip()
        return getattr(kb.Key, name) if hasattr(kb.Key, name) else kb.KeyCode.from_char(name)

    return frozenset(key_for(part) for part in hotkey.split("+"))


def wav_bytes(captured: list[Any], sample_rate: int) -> bytes:
    """Encode captured int16 mono frames as an in-memory PCM WAV."""
    import numpy as np

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(np.concatenate(captured, axis=0).tobytes())
    return buffer.getvalue()
