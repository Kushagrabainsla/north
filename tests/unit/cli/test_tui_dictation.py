"""Unit tests for TUI dictation logic."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli.tui import NorthApp


class _FakeResp:
    def __init__(self, js):
        self._js = js
        self.status_code = 200

    def json(self):
        return self._js

    def raise_for_status(self):
        pass


@pytest.mark.asyncio
async def test_toggle_dictation_starts_stops_transcribes() -> None:
    """Toggling dictation starts recording on first press, and stops/transcribes on second press."""
    app = NorthApp(
        base_url="http://127.0.0.1:8000",
        headers={"X-North-Secret": "test-secret"},
        workspace="/tmp/fake-workspace",
    )

    mock_sd = MagicMock()
    mock_np = MagicMock()
    mock_np.concatenate.return_value = MagicMock()
    mock_np.concatenate.return_value.tobytes.return_value = b"raw audio bytes"

    # Mock settings and endpoints
    mock_resp_transcribe = _FakeResp({"text": "Hello North, find the bug"})
    mock_resp_task = _FakeResp({"task_id": "task_12345"})

    # Mock HTTP client
    mock_client = AsyncMock()
    mock_client.post.side_effect = [mock_resp_transcribe, mock_resp_task]
    app._client = mock_client

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()

        with (
            patch("sounddevice.InputStream", return_value=mock_sd),
            patch("numpy.concatenate", return_value=mock_np.concatenate.return_value),
            patch("wave.open"),
        ):
            # 1. Start recording
            app._start_recording()
            assert app._recording is True
            assert app._audio_stream is not None

            # Simulate audio callback appending a frame
            app._audio_frames.append(b"frame")

            # 2. Stop recording
            app._stop_recording()
            assert app._recording is False
            assert app._audio_stream is None

            # Wait for the async task submission worker to finish
            await asyncio.sleep(0.1)

            # Assert transcription endpoint was called
            mock_client.post.assert_any_call(
                "http://127.0.0.1:8000/orchestrator/transcribe",
                content=b"",  # mocked wave buffer value
                headers={"X-North-Secret": "test-secret", "Content-Type": "audio/wav"},
                timeout=60.0,
            )

            # Assert the transcribed text was submitted as a task
            mock_client.post.assert_any_call(
                "http://127.0.0.1:8000/orchestrator/task",
                headers={"X-North-Secret": "test-secret"},
                json={
                    "prompt": "Hello North, find the bug",
                    "workspace": "/tmp/fake-workspace",
                },
                timeout=30.0,
            )
