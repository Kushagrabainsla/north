from __future__ import annotations

from tools.models import ToolInput
from tools.universal.get_active_sessions import GetActiveSessionsTool


class _Sessions:
    def __init__(self, sessions: list[dict] | None = None) -> None:
        self.sessions = sessions or []
        self.excluded: str | None = None

    async def list_active(self, exclude_task_id: str | None = None) -> list[dict]:
        self.excluded = exclude_task_id
        return self.sessions


async def test_active_sessions_uses_injected_read_port() -> None:
    store = _Sessions([{"task_id": "other", "domain": "news", "description": "digest", "started_at": ""}])
    out = await GetActiveSessionsTool(store).run(ToolInput(params={"task_id": "self"}))

    assert out.success
    assert store.excluded == "self"
    assert out.data["sessions"][0]["task_id"] == "other"


async def test_active_sessions_reports_unwired_store() -> None:
    out = await GetActiveSessionsTool().run(ToolInput(params={}))

    assert not out.success
    assert "not connected" in (out.error or "")
