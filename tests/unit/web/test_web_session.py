from __future__ import annotations

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from utils.security import verify_api_access
from web.api import session_router


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(session_router)

    @app.get("/protected", dependencies=[Depends(verify_api_access)])
    async def protected_get() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/protected", dependencies=[Depends(verify_api_access)])
    async def protected_post() -> dict[str, bool]:
        return {"ok": True}

    return app


def test_local_web_session_is_silent_and_csrf_protects_mutations() -> None:
    client = TestClient(_app(), base_url="http://127.0.0.1")
    session = client.post("/web/session")
    assert session.status_code == 200
    assert "north_web_session" in client.cookies

    assert client.get("/protected").status_code == 200
    assert client.post("/protected").status_code == 403
    assert client.post("/protected", headers={"X-North-CSRF": session.json()["csrf"]}).status_code == 200


def test_non_loopback_host_cannot_bootstrap_web_session() -> None:
    client = TestClient(_app(), base_url="http://example.test")
    assert client.post("/web/session").status_code == 403
