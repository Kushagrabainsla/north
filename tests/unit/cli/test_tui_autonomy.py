"""The TUI shows the approval mode the settings API reports, and answers no card itself.

`--yolo` used to be a second yes-to-everything path inside the terminal client,
so Telegram and the dashboard never honoured it. Now the mode lives on the
server; the TUI only shows it (#28).
"""

from __future__ import annotations

import contextlib
from unittest.mock import AsyncMock

from cli.tui import NorthApp
from config.approval_mode import mode_options

_DEAD = "http://127.0.0.1:1"


class _Resp:
    status_code = 200

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        pass


def _serve_settings(app: NorthApp, autonomy: str) -> None:
    settings = {"power": "sport", "autonomy": autonomy, "autonomy_options": mode_options()}

    class _Client:
        async def get(self, url, **kw):
            return _Resp(settings)

        async def post(self, url, **kw):
            settings.update(kw.get("json") or {})
            return _Resp(settings)

    @contextlib.asynccontextmanager
    async def fake_http():
        yield _Client()

    app._http = fake_http  # type: ignore[method-assign]


def _status_bar(app: NorthApp) -> str:
    return str(app.query_one("#statusbar").content)


async def test_the_yolo_badge_follows_the_mode_the_api_reports() -> None:
    app = NorthApp(base_url=_DEAD, headers={})
    async with app.run_test(size=(120, 30)) as pilot:
        _serve_settings(app, "yolo")
        await app._refresh_dials()
        await pilot.pause()
        assert "YOLO" in _status_bar(app)
        assert app._strategy == "sport"

        _serve_settings(app, "ask")
        await app._refresh_dials()
        await pilot.pause()
        assert "YOLO" not in _status_bar(app)


async def test_setting_yolo_from_the_tui_shows_the_badge() -> None:
    app = NorthApp(base_url=_DEAD, headers={})
    async with app.run_test(size=(120, 30)) as pilot:
        _serve_settings(app, "ask")
        await app._slash_autonomy("/autonomy yolo")
        await pilot.pause()
        assert app._autonomy == "yolo"
        assert "YOLO" in _status_bar(app)


async def test_autonomy_with_no_mode_lists_the_modes_the_api_serves() -> None:
    app = NorthApp(base_url=_DEAD, headers={})
    logged: list[str] = []
    app._log = logged.append  # type: ignore[method-assign]
    async with app.run_test(size=(120, 30)):
        _serve_settings(app, "safe")
        await app._slash_autonomy("/autonomy")
    text = "\n".join(logged)
    for option in mode_options():
        assert option["value"] in text and option["description"] in text


async def test_an_approval_card_waits_for_the_user_whatever_the_mode() -> None:
    """Under yolo the approval layer answers before a card is raised; one that
    reaches the TUI is for the user, never answered by the client."""
    app = NorthApp(base_url=_DEAD, headers={})
    app._autonomy = "yolo"
    app._log = lambda *a, **k: None  # type: ignore[method-assign]
    app._log_rich = lambda *a, **k: None  # type: ignore[method-assign]
    app._set_status = lambda *a, **k: None  # type: ignore[method-assign]
    app._submit_approval = AsyncMock()  # type: ignore[method-assign]

    await app._on_approval_required("t1", {"card_id": "c1", "message": "run it?", "options": ["Approve", "Reject"]})
    await app._on_question_required("t1", {"card_id": "c2", "question": "which?", "options": ["a", "b"]})

    app._submit_approval.assert_not_awaited()
