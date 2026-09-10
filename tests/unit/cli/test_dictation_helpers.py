from __future__ import annotations

import io
import wave

import numpy as np

from cli.dictation import wav_bytes


def test_wav_bytes_encodes_int16_mono_frames() -> None:
    encoded = wav_bytes([np.array([[1], [2]], dtype=np.int16)], 16_000)

    with wave.open(io.BytesIO(encoded), "rb") as wav:
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getframerate() == 16_000
        assert wav.readframes(2) == np.array([[1], [2]], dtype=np.int16).tobytes()
